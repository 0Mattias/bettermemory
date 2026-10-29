"""The membership CLI (RS-F6; design 3.4, 8.8 and 8.10; decisions D14 and
D18; the owner's ruling of 2026-10-21): `bettermemory project list | show
| why | pending | join | new | init`, `bettermemory memory move`,
`bettermemory membership migrate`, and the installer's removal of the
deny rules an earlier candidate added.

Each test runs the CLI in process on a scratch store. The caller's
resolution (`location.resolve_here`) is replaced by the model resolving a
location built by hand for a real directory under `tmp_path`: its git
directory's object, its token and its project file are read from disk,
while its commits live in an in-memory history, so no git process runs
and nothing depends on the host's git configuration. A declaration runs
from any stdin: `typed` gives it a terminal and `piped` a pipe, and
neither changes what it does. Tests are named by their design section 7
rows where one applies.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bettermemory import checkout_token, location, project_file
from bettermemory import membership as mb
from bettermemory.config import load_config
from bettermemory.membership import (
    ASK,
    FOLDER,
    GLOBAL,
    NEW,
    OUTSIDE,
    PRIMARY,
    PROMPT_RECALL,
    REPO,
    SEARCH_AUTO_SCOPE,
    SEARCH_UNSCOPED,
    SESSION_BRIEF,
    SILENT,
    SURFACES,
    Home,
    Location,
    Membership,
    ProjectFileRef,
    Resolution,
    admitted,
)
from bettermemory.models import Confidence, Memory, Source, generate_ulid
from bettermemory.origin import Origin
from bettermemory.server import main as cli_main
from bettermemory.store import RecordHome, Store
from bettermemory.location_git import reachable_among

_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# A scene: one store, this process's machine, repositories on disk
# ---------------------------------------------------------------------------


class History:
    """A repository's commits as `membership_records.HistoryReader` asks for
    them."""

    def __init__(self, commits: dict[str, tuple[str, ...]] | None = None) -> None:
        self.commits: dict[str, tuple[str, ...]] = dict(commits or {})

    def copy(self) -> History:
        return History(self.commits)

    def holds(self, commits: Sequence[str]) -> set[str]:
        return {c for c in commits if c in self.commits}

    def holds_objects(self, ids: Sequence[str]) -> set[str]:
        return self.holds(ids)

    def fingerprint(self, commit: str) -> str | None:
        return (
            hashlib.sha256(commit.encode()).hexdigest()
            if commit in self.commits
            else None
        )

    def parents(self, commit: str) -> Sequence[str]:
        return self.commits.get(commit, ())

    def reachable_among(self, commits: Sequence[str]) -> dict[str, frozenset[str]]:
        return reachable_among(self, commits)

    def blobs(self, commit: str) -> Sequence[str]:
        return ()


def _sha(label: str) -> str:
    return hashlib.sha1(f"{label}-{next(_counter)}".encode()).hexdigest()


def _object(path: str) -> mb.ObjectKey | None:
    """`path`'s filesystem object as the capture reads it."""
    found = location.object_key(path)
    return None if found is None else mb.ObjectKey(*found)


def file_ref(path: Path) -> ProjectFileRef | None:
    """The project file at `path`'s directory as the capture reports it."""
    found = project_file.parse(path / project_file.PROJECT_FILE_NAME)
    if not isinstance(found, project_file.ProjectFile):
        return None
    return ProjectFileRef(
        id=found.id,
        name=found.name,
        dir=str(path),
        path=str(found.path),
        boundary=found.boundary,
        object_key=_object(str(path)),
    )


@dataclass
class Repo:
    """A repository whose git directory and working tree are on disk and
    whose commits are in memory."""

    path: Path
    history: History
    head: str | None = None
    remotes: set[str] = field(default_factory=set)
    shallow: bool = False
    evicted: bool = False
    at: Path | None = None

    @property
    def git_dir(self) -> Path:
        return self.path / ".git"

    def commit(self) -> str:
        sha = _sha(str(self.path))
        self.history.commits[sha] = (self.head,) if self.head else ()
        self.head = sha
        return sha

    def location(self, machine: str, **overrides: Any) -> Location:
        """What the capture reads here, the checkout token written at first
        sight as the capture writes it."""
        wrote = checkout_token.write(self.git_dir, policy=True, bare=False)
        token = checkout_token.read(self.git_dir).token
        ref = file_ref(self.path)
        cwd = self.at or self.path
        nested = [
            found
            for folder in [cwd, *cwd.parents]
            if folder != self.path
            and self.path in folder.parents
            and (found := file_ref(folder)) is not None
        ]
        fields: dict[str, Any] = dict(
            machine=machine,
            cwd=str(cwd),
            state=REPO,
            root=str(self.path),
            kind=PRIMARY,
            git_dir=str(self.git_dir),
            object_key=_object(str(self.git_dir)),
            common_object_key=_object(str(self.git_dir)),
            root_object_key=_object(str(self.path)),
            head=self.head,
            ref_tips=(self.head,) if self.head else (),
            remote_keys=frozenset(self.remotes),
            shallow=self.shallow,
            token=token if wrote.status != "written" else None,
            token_written=token if wrote.status == "written" else None,
            token_status="written" if wrote.status == "written" else "present",
            files=(*nested, *((ref,) if ref is not None else ())),
            file_tracked=True if ref is not None else None,
            evicted=self.evicted,
            detail="HEAD" if self.evicted else "",
            history=self.history,
        )
        fields.update(overrides)
        return Location(**fields)


def folder_location(path: Path, machine: str) -> Location:
    """A plain folder: the folder checkout of the nearest project file at
    it or above (the outer files enclose it), else outside any project."""
    found = [ref for folder in (path, *path.parents) if (ref := file_ref(folder))]
    if not found:
        return Location(machine=machine, cwd=str(path), state=OUTSIDE)
    return Location(
        machine=machine,
        cwd=str(path),
        state=OUTSIDE,
        root=found[0].dir,
        kind=FOLDER,
        object_key=_object(found[0].dir),
        files=tuple(found),
    )


class Scene:
    """The CLI's store (`BETTERMEMORY_DIR`), the model of this process's
    machine over it, and where the caller stands (`here`)."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        monkeypatch.setenv("BETTERMEMORY_DIR", str(root / "store"))
        self.store = Store(load_config().resolved_directory())
        self.identity = location.process_machine()
        self.machine = self.identity.id
        Membership.open(self.store, self.identity)
        self.here: Repo | Path | None = None
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(location, "resolve_here", self.resolve_here)

    @property
    def model(self) -> Membership:
        """A model read afresh: the CLI writes through models of its own."""
        return Membership(self.store, self.machine)

    def close(self) -> None:
        self.store.close()

    def location_of(self, where: Repo | Path | None) -> Location:
        if isinstance(where, Repo):
            return where.location(self.machine)
        if isinstance(where, Path):
            return folder_location(where, self.machine)
        return Location(machine=self.machine, cwd=str(self.root), state=OUTSIDE)

    def resolve_here(
        self,
        store: Store,
        config: object,
        cwd: object,
        *,
        session: str | None = None,
        **_: object,
    ) -> Resolution:
        """`location.resolve_here` for the CLI: the model over the CLI's own
        store resolving where the scene stands."""
        return Membership(store, self.machine).resolve(self.location_of(self.here))

    def repo(self, name: str, *, commits: int = 2, remote: str | None = None) -> Repo:
        path = self.root / name
        (path / ".git" / "info").mkdir(parents=True)
        repo = Repo(path=path, history=History(), remotes={remote} if remote else set())
        for _ in range(commits):
            repo.commit()
        return repo

    def clone(self, repo: Repo, name: str, *, remote: str | None = None) -> Repo:
        """A fresh clone: the history, another git directory, no token."""
        twin = self.repo(name, commits=0)
        twin.history = repo.history.copy()
        twin.head = repo.head
        twin.remotes = {remote} if remote else set(repo.remotes)
        return twin

    def see(self, where: Repo | Path | None) -> Resolution:
        """A sighting, which leaves the caller standing there."""
        self.here = where
        self.monkeypatch.chdir(
            (where.at or where.path) if isinstance(where, Repo) else where or self.root
        )
        return self.model.resolve(self.location_of(where))

    def write(self, res: Resolution, kind: str = "memory") -> str:
        home = res.home
        assert home is not None
        return self.put(home, kind)

    def put(self, home: RecordHome | None, kind: str = "memory") -> str:
        rid = generate_ulid()
        if kind == "memory":
            self.store.put_memory(
                Memory(
                    id=rid,
                    created=_NOW,
                    updated=_NOW,
                    scopes=["tools"],
                    confidence=Confidence.MEDIUM,
                    source=Source.EXPLICIT,
                    body=f"memory {rid}\n",
                ),
                home=home,
            )
        else:
            from bettermemory.models import Episode

            self.store.put_episode(
                Episode(id=rid, session_id="s", created=_NOW, body=f"episode {rid}\n"),
                home=home,
            )
        return rid

    def rows(self, kind: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self.store.membership_log([kind]):
            payload = json.loads(row.payload)
            assert isinstance(payload, dict)
            out.append(payload)
        return out

    def tables(self) -> dict[str, list[dict[str, Any]]]:
        return {
            table: self.store.membership_rows(table)
            for table in (
                "projects",
                "checkouts",
                "proposals",
                "homes",
                "project_files",
            )
        }


@pytest.fixture
def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scene]:
    monkeypatch.setattr("bettermemory.cli.project_cmd.principal", lambda: "os:tester")
    made = Scene(tmp_path.resolve(), monkeypatch)
    yield made
    made.close()


class _Terminal(io.StringIO):
    """A stdin that is a terminal, holding what the user types."""

    def isatty(self) -> bool:
        return True


def typed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", _Terminal(text + "\n"))


def piped(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text + "\n"))


def bm(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """`bettermemory <argv>` in process."""
    monkeypatch.setattr(sys, "argv", ["bettermemory", *argv])
    cli_main()


def refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> str:
    """Run a command that must exit 2; returns its one stderr line."""
    with pytest.raises(SystemExit) as exc:
        bm(monkeypatch, *argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1, err
    return err


URL = "example.invalid/acme/app"


def _upstream_and_fork(scene: Scene) -> tuple[Resolution, str, Repo, Resolution]:
    """A project with a memory, and a fork under its own remote: the fork
    holds the project's commits only (one witness), so it asks (T4)."""
    upstream = scene.repo("app", remote=URL)
    up = scene.see(upstream)
    rid = scene.write(up)
    fork = scene.clone(upstream, "fork", remote="example.invalid/me/app")
    asked = scene.see(fork)
    assert asked.outcome == ASK and asked.proposal is not None
    return up, rid, fork, asked


# ---------------------------------------------------------------------------
# Reading: list, show, why, pending
# ---------------------------------------------------------------------------


def test_project_list_shows_projects_checkouts_and_live_views(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    up, _rid, fork, asked = _upstream_and_fork(scene)
    capsys.readouterr()
    bm(monkeypatch, "project", "list")
    out = capsys.readouterr().out
    assert f'{up.project} "app"\n' in out
    assert f"  {up.checkout} primary, live at {scene.root / 'app'}" in out
    assert f'{asked.project} "fork" [provisional: one_witness]' in out
    assert f"  {asked.checkout} primary, live at {fork.path}" in out


def test_project_list_marks_an_unresolved_project(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blind = Location(
        machine=scene.machine, cwd=str(scene.root / "x"), state=mb.UNREADABLE
    )
    scene.model.resolve(blind)
    scene.put(scene.model.unresolved_home(blind, session="s1"))
    bm(monkeypatch, "project", "list")
    out = capsys.readouterr().out
    assert '"x" [unresolved: indeterminate]' in out
    assert (
        f"unresolved, written at {scene.root / 'x'} while it could not be read" in out
    )


def test_project_list_on_an_empty_store(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bm(monkeypatch, "project", "list")
    assert capsys.readouterr().out == "bettermemory: no projects yet\n"


def test_project_show_prints_the_evidence_and_open_questions(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    up, _rid, _fork, asked = _upstream_and_fork(scene)
    capsys.readouterr()
    bm(monkeypatch, "project", "show", str(up.project))
    out = capsys.readouterr().out
    assert out.startswith(f'{up.project} "app"\n')
    assert "  witnesses: 1 commits\n" in out
    assert f"  remotes: {URL}\n" in out
    assert "  file ids: (none)\n" in out
    assert "  records: 1 memories, 0 episodes\n" in out
    assert f"    {up.checkout} primary, live at" in out
    assert "  open questions (1):" in out
    assert f"    {asked.proposal} in checkout {asked.checkout}: bettermemory:" in out


def test_project_show_refuses_an_unknown_id(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    err = refused(monkeypatch, capsys, "project", "show", "P999")
    assert "no project P999" in err


def test_project_pending_prints_each_question_its_line_and_where(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    up, _rid, fork, asked = _upstream_and_fork(scene)
    capsys.readouterr()
    bm(monkeypatch, "project", "pending")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(
        f"{asked.proposal} in checkout {asked.checkout} at {fork.path} "
        "(first_sight: one_witness), opened "
    )
    assert out[1].startswith(f"  bettermemory: {fork.path} may be project")
    assert (
        f'Same project, when the user says so: memory_admin(action="answer_question", '
        f'id="{asked.proposal}", answer="join", target="{up.project}", '
        'said="<the user\'s words>")' in out[1]
    )
    assert out[1].endswith(
        f'separate: memory_admin(action="answer_question", id="{asked.proposal}", '
        'answer="separate")'
    )


def test_project_pending_says_when_nothing_is_open(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bm(monkeypatch, "project", "pending")
    assert capsys.readouterr().out == "bettermemory: no open questions\n"


def test_x13_project_why_prints_project_checkout_rule_and_rows(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    up, rid, _fork, _asked = _upstream_and_fork(scene)
    capsys.readouterr()
    bm(monkeypatch, "project", "why", rid)
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(
        f'{rid}: project "app" ({up.project}), written in checkout {up.checkout}; '
        "rule new"
    )
    kinds = [line.split()[3] for line in out[1:]]
    for kind in ("checkout_registered", "home_put", "project_minted"):
        assert kind in kinds
    bm(monkeypatch, "project", "why", "01NOTARECORD0000000000000")
    assert "no home recorded" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Declarations run from any stdin (the owner's ruling of 2026-10-21)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stdin", [typed, piped], ids=["terminal", "pipe"])
def test_declarations_run_from_any_stdin_and_type_nothing(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: Any,
) -> None:
    """A declaration neither needs a terminal nor asks for a name: an
    agent's shell runs it as a terminal does, and stdin is never read."""
    up, _rid, fork, asked = _upstream_and_fork(scene)
    stdin(monkeypatch, "not the project's name")
    bm(monkeypatch, "project", "join", str(up.project))
    out = capsys.readouterr().out
    assert "to confirm" not in out and "terminal" not in out
    assert f"question {asked.proposal} answered" in out
    pr = scene.model.proposal(str(asked.proposal))
    assert (pr.state, pr.answer, pr.answered_by) == (
        mb.ANSWERED,
        f"join:{up.project}",
        "declaration:os:tester",
    )
    assert scene.see(fork).project == up.project


def test_join_refuses_an_unknown_project_and_a_folder_outside_any_checkout(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _upstream_and_fork(scene)
    typed(monkeypatch, "x")
    assert "no project P404" in refused(monkeypatch, capsys, "project", "join", "P404")
    plain = scene.root / "notes"
    plain.mkdir()
    scene.see(plain)
    typed(monkeypatch, "x")
    err = refused(monkeypatch, capsys, "project", "new")
    assert "is no checkout" in err and "project init" in err


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


def test_t04_join_answers_the_open_question(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    up, rid, fork, asked = _upstream_and_fork(scene)
    mine = scene.write(asked)
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "join", str(up.project))
    out = capsys.readouterr().out
    assert f"answers question {asked.proposal} in checkout {asked.checkout}" in out
    assert (
        f'bettermemory: checkout {asked.checkout} is project "app" ({up.project}); '
        f"question {asked.proposal} answered"
    ) in out
    pr = scene.model.proposal(str(asked.proposal))
    assert (pr.state, pr.answer, pr.answered_by) == (
        mb.ANSWERED,
        f"join:{up.project}",
        "declaration:os:tester",
    )
    again = scene.see(fork)
    assert (again.outcome, again.project) == (SILENT, up.project)
    homes = scene.model.homes([rid, mine])
    assert {h.project for h in homes.values()} == {up.project}


def test_join_without_a_question_moves_the_checkout_with_a_principal_row(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a = scene.see(scene.repo("a"))
    b_repo = scene.repo("b")
    b = scene.see(b_repo)
    rid = scene.write(b)
    assert (a.outcome, b.outcome) == (NEW, NEW)
    typed(monkeypatch, "a")
    bm(monkeypatch, "project", "join", str(a.project))
    out = capsys.readouterr().out
    assert f'leaves "b" ({b.project}) and joins "a" ({a.project})' in out
    assert scene.model.canonical(b.project) == a.project
    assert scene.see(b_repo).project == a.project
    home = scene.model.home(rid)
    assert home is not None and home.project == a.project
    declared = scene.rows("resolution_joined")[-1]
    assert declared == {
        "checkout": b.checkout,
        "project": a.project,
        "witnesses": ["declaration"],
        "principal": "os:tester",
        "proposal": None,
    }
    typed(monkeypatch, "a")
    bm(monkeypatch, "project", "join", str(a.project))
    assert "is already project" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------


def test_new_answers_separate_and_remints_the_token(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    up, _rid, fork, asked = _upstream_and_fork(scene)
    token = checkout_token.read(fork.git_dir).token
    typed(monkeypatch, "fork")
    bm(monkeypatch, "project", "new")
    out = capsys.readouterr().out
    assert "with: separate, a project of its own" in out
    assert out.rstrip().endswith(
        f"question {asked.proposal} answered; its checkout token was re-minted"
    )
    pr = scene.model.proposal(str(asked.proposal))
    assert (pr.answer, pr.answered_by) == ("new", "declaration:os:tester")
    assert checkout_token.read(fork.git_dir).token not in (None, token)
    again = scene.see(fork)
    assert again.outcome == SILENT and again.project == asked.project
    assert scene.model.project(str(asked.project)).provisional is None
    assert scene.model.pending() == []


def test_new_without_a_question_splits_the_checkout(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream = scene.repo("app", remote=URL)
    up = scene.see(upstream)
    copy = scene.clone(upstream, "copy")
    joined = scene.see(copy)
    assert (joined.outcome, joined.project) == (SILENT, up.project)
    rid = scene.write(joined)
    typed(monkeypatch, "copy")
    bm(monkeypatch, "project", "new")
    out = capsys.readouterr().out
    assert (
        f'leaves "app" ({up.project}) and becomes a project of its own, "copy"' in out
    )
    own = scene.see(copy)
    assert own.project not in (None, up.project)
    home = scene.model.home(rid)
    assert home is not None and home.project == own.project
    declared = scene.rows("resolution_new")[-1]
    assert (declared["principal"], declared["project"]) == ("os:tester", own.project)
    assert scene.see(upstream).project == up.project


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_t02_init_writes_the_file_and_binds_it_to_the_checkouts_project(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream = scene.repo("app", remote=URL)
    up = scene.see(upstream)
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "init")
    out = capsys.readouterr().out
    written = project_file.parse(upstream.path / project_file.PROJECT_FILE_NAME)
    assert isinstance(written, project_file.ProjectFile)
    assert written.name == "app"
    assert f'bound to project "app" ({up.project}); commit it' in out
    bound = scene.store.membership_rows("project_files", where={"file_id": written.id})
    assert bound[0]["project"] == up.project
    assert "declaration:os:tester" in bound[0]["evidence"]
    # A teammate's clone carrying the committed file joins silently (T2).
    clone = scene.clone(upstream, "teammate")
    (clone.path / project_file.PROJECT_FILE_NAME).write_bytes(
        (upstream.path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    res = scene.see(clone)
    assert (res.outcome, res.project, res.rule) == (SILENT, up.project, "join:F+H+R")


def test_init_refuses_an_existing_file_and_rebind_without_one(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = scene.repo("app")
    scene.see(repo)
    typed(monkeypatch, "app")
    err = refused(monkeypatch, capsys, "project", "init", "--rebind")
    assert "no " in err and "to rebind" in err
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "init")
    capsys.readouterr()
    typed(monkeypatch, "app")
    err = refused(monkeypatch, capsys, "project", "init")
    assert "exists" in err and "--rebind" in err and "--new" in err


def test_t08_init_new_makes_a_template_clone_a_product_of_its_own(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template = scene.repo("template", remote="example.invalid/acme/template")
    first = scene.see(template)
    typed(monkeypatch, "template")
    bm(monkeypatch, "project", "init")
    template_file = (template.path / project_file.PROJECT_FILE_NAME).read_bytes()
    product = scene.clone(template, "product")
    (product.path / project_file.PROJECT_FILE_NAME).write_bytes(template_file)
    joined = scene.see(product)
    assert (joined.outcome, joined.project) == (SILENT, first.project)
    capsys.readouterr()
    typed(monkeypatch, "product")
    bm(monkeypatch, "project", "init", "--new")
    out = capsys.readouterr().out
    assert "replaces" in out and "a project of its own" in out
    own = project_file.parse(product.path / project_file.PROJECT_FILE_NAME)
    assert isinstance(own, project_file.ProjectFile)
    assert own.path.read_bytes() != template_file
    res = scene.see(product)
    assert res.outcome == SILENT and res.project not in (None, first.project)
    # The product's fresh clone carries the product's file: the product's.
    fresh = scene.clone(product, "fresh")
    (fresh.path / project_file.PROJECT_FILE_NAME).write_bytes(own.path.read_bytes())
    assert scene.see(fresh).project == res.project
    assert scene.see(template).project == first.project


def test_t10a_use_this_template_asks_and_init_new_answers(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    template = scene.repo("kit", remote="example.invalid/acme/kit")
    first = scene.see(template)
    typed(monkeypatch, "kit")
    bm(monkeypatch, "project", "init")
    product = scene.repo("product", remote="example.invalid/me/product")
    (product.path / project_file.PROJECT_FILE_NAME).write_bytes(
        (template.path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    asked = scene.see(product)
    assert asked.outcome == ASK
    assert asked.lines[-1].endswith(
        "separate: bettermemory project init --new (a fresh project file)"
    )
    capsys.readouterr()
    typed(monkeypatch, "product")
    bm(monkeypatch, "project", "init", "--new")
    out = capsys.readouterr().out
    assert f"answers question {asked.proposal}" in out
    pr = scene.model.proposal(str(asked.proposal))
    assert (pr.answer, pr.answered_by) == ("new", "declaration:os:tester")
    res = scene.see(product)
    assert (res.outcome, res.project) == (SILENT, asked.project)
    assert scene.see(template).project == first.project


def test_l3_init_rebind_keeps_the_checkouts_project(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a = scene.repo("a")
    pa = scene.see(a)
    typed(monkeypatch, "a")
    bm(monkeypatch, "project", "init")
    b = scene.repo("b")
    pb = scene.see(b)
    (b.path / project_file.PROJECT_FILE_NAME).write_bytes(
        (a.path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    b.commit()
    asked = scene.see(b)
    assert asked.outcome == ASK and asked.project == pb.project
    assert "Keep: bettermemory project init --rebind" in asked.lines[-1]
    capsys.readouterr()
    typed(monkeypatch, "b")
    bm(monkeypatch, "project", "init", "--rebind")
    out = capsys.readouterr().out
    assert '(it names "a",' in out
    rebound = project_file.parse(b.path / project_file.PROJECT_FILE_NAME)
    assert isinstance(rebound, project_file.ProjectFile)
    assert (
        scene.store.membership_rows("project_files", where={"file_id": rebound.id})[0][
            "project"
        ]
        == pb.project
    )
    assert scene.model.pending() == []
    res = scene.see(b)
    assert (res.outcome, res.project) == (SILENT, pb.project)
    assert scene.see(a).project == pa.project


def test_i29_init_in_a_plain_folder_makes_a_folder_project(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = scene.root / "notes"
    notes.mkdir()
    outside = scene.see(notes)
    assert outside.project == GLOBAL
    typed(monkeypatch, "notes")
    bm(monkeypatch, "project", "init", "--boundary")
    out = capsys.readouterr().out
    assert f"and makes {notes} a folder project" in out
    assert f"bettermemory: {notes} is a new project" in out
    written = project_file.parse(notes / project_file.PROJECT_FILE_NAME)
    assert isinstance(written, project_file.ProjectFile) and written.boundary
    res = scene.see(notes)
    assert res.outcome == SILENT and res.project not in (None, GLOBAL)
    project = scene.model.project(str(res.project))
    assert project.folder and project.boundary
    bound = scene.store.membership_rows("project_files", where={"file_id": written.id})
    assert "declaration:os:tester" in bound[0]["evidence"]


def _home(scene: Scene, record_id: str) -> Home:
    found = scene.model.home(record_id)
    assert found is not None
    return found


def test_x05_d13_init_below_the_root_makes_a_sub_project(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono = scene.repo("mono", remote=URL)
    root = scene.see(mono)
    assert root.project is not None
    name = scene.model.project(root.project).name
    at_root = scene.write(root)
    parts: dict[str, tuple[Resolution, str]] = {}
    for part in ("web", "api"):
        mono.at = mono.path / "packages" / part
        mono.at.mkdir(parents=True)
        assert scene.see(mono).project == root.project
        capsys.readouterr()
        typed(monkeypatch, part)
        bm(monkeypatch, "project", "init")
        out = capsys.readouterr().out
        assert (
            f'and makes {mono.at} a sub-project "{part}" of "{name}" ({root.project})'
        ) in out
        written = project_file.parse(mono.at / project_file.PROJECT_FILE_NAME)
        assert isinstance(written, project_file.ProjectFile)
        bound = scene.store.membership_rows(
            "project_files", where={"file_id": written.id}
        )
        sub = bound[0]["project"]
        assert sub != root.project
        assert "declaration:os:tester" in bound[0]["evidence"]
        assert f'a sub-project "{part}" ({sub}) of "{name}" ({root.project})' in out
        assert scene.model.project(sub).parent == root.project
        res = scene.see(mono)
        assert (res.outcome, res.project, res.checkout) == (
            SILENT,
            sub,
            root.checkout,
        )
        assert res.chain[:2] == (sub, root.project)
        parts[part] = (res, scene.write(res))
    # The checkout stays the repository's; only the folders are parts of it.
    assert root.checkout is not None
    assert scene.model.checkout(root.checkout).project == root.project
    # X5: a session in a part gets its own and the root's, never a sibling's.
    web, mine = parts["web"]
    _api, sibling = parts["api"]
    for surface in (SESSION_BRIEF, PROMPT_RECALL, SEARCH_AUTO_SCOPE):
        assert admitted(surface, _home(scene, mine), web)
        assert admitted(surface, _home(scene, at_root), web)
        assert not admitted(surface, _home(scene, sibling), web)
    # The root's search reaches its parts (D13); its brief does not.
    mono.at = None
    top = scene.see(mono)
    assert top.project == root.project
    assert admitted(SEARCH_AUTO_SCOPE, _home(scene, mine), top)
    assert not admitted(SESSION_BRIEF, _home(scene, mine), top)


def test_d13_init_below_a_part_names_a_part_of_the_next_file_up(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono = scene.repo("mono")
    root = scene.see(mono)
    web = mono.path / "packages" / "web"
    docs = web / "docs"
    docs.mkdir(parents=True)
    mono.at = web
    scene.see(mono)
    typed(monkeypatch, "web")
    bm(monkeypatch, "project", "init")
    outer = scene.see(mono)
    mono.at = docs
    assert scene.see(mono).project == outer.project
    capsys.readouterr()
    typed(monkeypatch, "docs")
    bm(monkeypatch, "project", "init")
    out = capsys.readouterr().out
    assert f'a sub-project "docs" of "web" ({outer.project})' in out
    res = scene.see(mono)
    assert res.outcome == SILENT
    assert scene.model.chain_of(res.project) == [
        res.project,
        outer.project,
        root.project,
    ]


def test_b17_init_new_below_the_root_makes_a_part_and_says_the_checkout_stays(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Decision B17: `project init --new` below a repository's root with no
    question open: the folder becomes a part with a fresh id, the checkout
    keeps its project and is not split, and the command's lines say so."""
    mono = scene.repo("mono", remote=URL)
    root = scene.see(mono)
    assert root.project is not None and root.checkout is not None
    name = scene.model.project(root.project).name
    cli = mono.path / "packages" / "cli"
    out = _init_at(scene, monkeypatch, capsys, mono, cli, "--new")
    assert f'and makes {cli} a sub-project "cli" of "{name}" ({root.project})' in out
    keeps = f'checkout {root.checkout} keeps project "{name}" ({root.project})'
    assert f"  with an id of its own; {keeps}" in out
    written = project_file.parse(cli / project_file.PROJECT_FILE_NAME)
    assert isinstance(written, project_file.ProjectFile)
    sub = scene.model.file_binding(written.id)
    assert sub not in (None, root.project)
    assert f'a sub-project "cli" ({sub}) of "{name}" ({root.project})' in out
    assert keeps in out.splitlines()[-1]
    assert scene.model.project(str(sub)).parent == root.project
    assert scene.model.checkout(root.checkout).project == root.project
    assert [r["checkout"] for r in scene.rows("resolution_new")] == [root.checkout]
    assert scene.rows("token_reminted") == []
    res = scene.see(mono)
    assert (res.outcome, res.project, res.checkout) == (SILENT, sub, root.checkout)
    mono.at = None
    assert scene.see(mono).project == root.project


def test_i31_init_in_a_folder_inside_a_folder_project(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = scene.root / "notes"
    drafts = notes / "drafts"
    drafts.mkdir(parents=True)
    typed(monkeypatch, "notes")
    scene.see(notes)
    bm(monkeypatch, "project", "init")
    outer = scene.see(notes)
    assert scene.see(drafts).project == outer.project
    capsys.readouterr()
    typed(monkeypatch, "drafts")
    bm(monkeypatch, "project", "init")
    out = capsys.readouterr().out
    assert f"and makes {drafts} a folder project" in out
    res = scene.see(drafts)
    assert res.outcome == SILENT and res.project != outer.project
    assert scene.model.project(str(res.project)).folder
    assert scene.model.chain_of(res.project) == [res.project, outer.project]


def _init_at(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repo: Repo,
    folder: Path | None,
    *flags: str,
) -> str:
    """`bettermemory project init` run in `folder` of `repo` (its root when
    None), the folder's name typed; what it printed."""
    repo.at = folder
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
    scene.see(repo)
    capsys.readouterr()
    typed(monkeypatch, (folder or repo.path).name)
    bm(monkeypatch, "project", "init", *flags)
    return capsys.readouterr().out


def _sees(caller: Resolution, home: Home) -> list[str]:
    return [surface for surface in SURFACES if admitted(surface, home, caller)]


def test_b9_init_boundary_at_the_root_makes_the_repository_a_tenant(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = scene.repo("app", remote=URL)
    root = scene.see(app)
    assert root.project is not None
    at_root = scene.write(root)
    _init_at(scene, monkeypatch, capsys, app, app.path / "packages" / "web")
    web = scene.see(app)
    in_web = scene.write(web)
    other = scene.repo("other", remote="example.invalid/acme/other")
    theirs = scene.write(scene.see(other))
    out = _init_at(scene, monkeypatch, capsys, app, None, "--boundary")
    assert ", a boundary: nothing crosses it" in out
    project = scene.model.project(root.project)
    assert project.boundary and project.tenant == root.project
    # A part bound before the boundary sits inside it too.
    assert scene.model.project(str(web.project)).tenant == root.project
    app.at = None
    inside = scene.see(app)
    assert (inside.outcome, inside.project, inside.tenant) == (
        SILENT,
        root.project,
        root.project,
    )
    assert _sees(inside, _home(scene, at_root)) == list(SURFACES)
    assert SEARCH_AUTO_SCOPE in _sees(inside, _home(scene, in_web))
    app.at = app.path / "packages" / "web"
    assert SESSION_BRIEF in _sees(scene.see(app), _home(scene, at_root))
    # Nothing crosses it, the unscoped search included, either way.
    assert _sees(inside, _home(scene, theirs)) == []
    outside = scene.see(other)
    assert _sees(outside, _home(scene, at_root)) == []
    assert _sees(outside, _home(scene, in_web)) == []
    # A teammate's clone carrying the committed file joins inside it.
    clone = scene.clone(app, "teammate")
    (clone.path / project_file.PROJECT_FILE_NAME).write_bytes(
        (app.path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    joined = scene.see(clone)
    assert (joined.outcome, joined.project, joined.tenant) == (
        SILENT,
        root.project,
        root.project,
    )


def test_b9_init_boundary_below_the_root_fences_the_part(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono = scene.repo("mono", remote=URL)
    root = scene.see(mono)
    at_root = scene.write(root)
    _init_at(scene, monkeypatch, capsys, mono, mono.path / "packages" / "web")
    in_web = scene.write(scene.see(mono))
    out = _init_at(
        scene, monkeypatch, capsys, mono, mono.path / "packages" / "vault", "--boundary"
    )
    assert ", a boundary: nothing crosses it" in out
    vault = scene.see(mono)
    assert vault.project is not None and vault.tenant == vault.project
    project = scene.model.project(vault.project)
    assert project.boundary and project.parent == root.project
    in_vault = scene.write(vault)
    # The boundary outranks D13: the part sees nothing of the root, nor of
    # its sibling, on any surface; it sees its own.
    assert _sees(vault, _home(scene, at_root)) == []
    assert _sees(vault, _home(scene, in_web)) == []
    assert _sees(vault, _home(scene, in_vault)) == list(SURFACES)
    # And the root, which reaches its other part, sees nothing of it.
    mono.at = None
    top = scene.see(mono)
    assert _sees(top, _home(scene, in_vault)) == []
    assert SEARCH_UNSCOPED in _sees(top, _home(scene, in_web))
    assert SEARCH_AUTO_SCOPE in _sees(top, _home(scene, in_web))


def test_b9_the_resolver_carries_a_files_boundary_onto_what_it_binds(
    scene: Scene,
) -> None:
    vault = scene.repo("vault")
    project_file.write(vault.path, name="vault", boundary=True)
    part = vault.path / "keys"
    part.mkdir()
    project_file.write(part, name="keys", boundary=True)
    res = scene.see(vault)
    assert res.project is not None
    repo_project = scene.model.project(res.project)
    assert repo_project.boundary and repo_project.tenant == res.project
    vault.at = part
    inner = scene.see(vault)
    assert inner.outcome == SILENT and inner.project not in (None, res.project)
    sub = scene.model.project(str(inner.project))
    assert sub.boundary and sub.tenant == sub.id and sub.parent == res.project
    assert inner.tenant == sub.id


def test_b10_project_new_takes_the_checkouts_parts_along(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = scene.repo("app", remote=URL)
    root = scene.see(app)
    web = app.path / "packages" / "web"
    web.mkdir(parents=True)
    project_file.write(web, name="web")
    app.at = web
    seen = scene.see(app)
    assert scene.model.project(str(seen.project)).parent == root.project
    _init_at(scene, monkeypatch, capsys, app, app.path / "docs")
    docs = scene.see(app)
    app.at = None
    scene.see(app)
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "new")
    out = capsys.readouterr().out
    assert "and takes its parts along: " in out
    assert '"web"' in out and '"docs"' in out
    mine = scene.see(app)
    assert mine.project not in (None, root.project)
    for folder, part in ((web, seen.project), (app.path / "docs", docs.project)):
        app.at = folder
        res = scene.see(app)
        assert (res.outcome, res.project) == (SILENT, part)
        assert scene.model.chain_of(res.project) == [part, mine.project]
    assert scene.model.pending() == []
    moved = scene.rows("project_reparented")
    assert {row["project"] for row in moved} == {seen.project, docs.project}
    assert {row["parent"] for row in moved} == {mine.project}
    assert all(
        row["evidence"] == f"declaration:os:tester: bettermemory project new in "
        f"checkout {root.checkout}"
        for row in moved
    )


def _flag(folder: Path, boundary: bool) -> None:
    """Edit the folder's committed project file: the same id, the
    ``boundary`` flag set or removed."""
    path = folder / project_file.PROJECT_FILE_NAME
    found = project_file.parse(path)
    assert isinstance(found, project_file.ProjectFile)
    path.write_bytes(project_file.render(replace(found, boundary=boundary)))


def _reparented(scene: Scene) -> list[dict[str, Any]]:
    return scene.rows("project_reparented")


def test_b10_a_join_that_moves_the_checkout_takes_its_parts_along(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = scene.repo("app", remote=URL)
    first = scene.see(app)
    twin = scene.clone(app, "twin")
    assert scene.see(twin).project == first.project
    other = scene.see(scene.repo("other", remote="example.invalid/acme/other"))
    web = twin.path / "packages" / "web"
    web.mkdir(parents=True)
    project_file.write(web, name="web")
    twin.at = web
    part = scene.see(twin)
    assert part.project is not None
    rid = scene.write(part)
    twin.at = None
    scene.see(twin)
    capsys.readouterr()
    typed(monkeypatch, "other")
    bm(monkeypatch, "project", "join", str(other.project))
    out = capsys.readouterr().out
    assert f'and takes its parts along: "web" ({part.project}) at {web}' in out
    twin.at = web
    res = scene.see(twin)
    assert (res.outcome, res.project) == (SILENT, part.project)
    assert scene.model.chain_of(res.project) == [part.project, other.project]
    assert scene.model.pending() == []
    # The clone that stayed is untouched.
    assert scene.see(app).project == first.project
    (row,) = _reparented(scene)
    assert (row["project"], row["parent"], row["previous"]) == (
        part.project,
        other.project,
        first.project,
    )
    assert "declaration:os:tester: bettermemory project join" in row["evidence"]
    # B13: `why` on a record in the moved part shows that row.
    # The record written in the part stays in it, and `why` on it shows the
    # row (B13).
    home = scene.model.home(rid)
    assert home is not None and home.project == part.project
    assert "project_reparented" in [r.kind for r in scene.model.why(rid).rows]
    bm(monkeypatch, "project", "why", rid)
    assert " project_reparented " in capsys.readouterr().out


def test_b10_a_part_first_bound_in_another_clone_asks_once_after_new(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a = scene.repo("a", remote=URL)
    shared = scene.see(a)
    (a.path / "packages" / "web").mkdir(parents=True)
    project_file.write(a.path / "packages" / "web", name="web")
    b = scene.clone(a, "b")
    (b.path / "packages" / "web").mkdir(parents=True)
    for repo in (a, b):
        (repo.path / "packages" / "web" / project_file.PROJECT_FILE_NAME).write_bytes(
            (a.path / "packages" / "web" / project_file.PROJECT_FILE_NAME).read_bytes()
        )
    assert scene.see(b).project == shared.project
    a.at = a.path / "packages" / "web"
    part = scene.see(a)
    b.at = b.path / "packages" / "web"
    assert scene.see(b).project == part.project
    b.at = None
    scene.see(b)
    capsys.readouterr()
    typed(monkeypatch, "b")
    bm(monkeypatch, "project", "new")
    out = capsys.readouterr().out
    assert "takes its parts along" not in out
    mine = scene.see(b)
    assert _reparented(scene) == []
    # The part stays with the project that holds it; here it asks once (L7).
    b.at = b.path / "packages" / "web"
    asked = scene.see(b)
    assert (asked.outcome, asked.project) == (ASK, mine.project)
    line = asked.lines[-1]
    assert f'id="{asked.proposal}", answer="join", target="{part.project}"' in line
    assert "bettermemory project init --new (in that folder)" in line
    again = scene.see(b)
    assert again.proposal == asked.proposal
    assert len(scene.model.pending()) == 1
    # Clone A is untouched.
    res_a = scene.see(a)
    assert (res_a.outcome, res_a.project) == (SILENT, part.project)
    assert scene.model.chain_of(part.project) == [part.project, shared.project]
    # Either command settles it: a part of B's project with an id of its own.
    scene.see(b)
    capsys.readouterr()
    typed(monkeypatch, "web")
    bm(monkeypatch, "project", "init", "--new")
    settled = scene.see(b)
    assert settled.outcome == SILENT and settled.project != part.project
    assert scene.model.chain_of(settled.project) == [settled.project, mine.project]
    assert scene.model.pending() == []


def test_b11_new_refuses_a_checkout_whose_root_file_is_bound(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = scene.repo("app", remote=URL)
    _init_at(scene, monkeypatch, capsys, app, None)
    scene.see(app)
    tables = scene.tables()
    log = len(list(scene.store.membership_log()))
    typed(monkeypatch, "app")
    err = refused(monkeypatch, capsys, "project", "new")
    assert "bettermemory project init --new" in err and "nothing changed" in err
    assert scene.tables() == tables
    assert not [
        row
        for row in list(scene.store.membership_log())[log:]
        if row.kind in ("resolution_new", "proposal_answered", "project_minted")
    ]


def test_b12_a_root_files_boundary_follows_the_file_and_rebind_lifts_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app = scene.repo("app", remote=URL)
    _init_at(scene, monkeypatch, capsys, app, None)
    root = scene.see(app)
    assert root.project is not None
    at_root = scene.write(root)
    other = scene.repo("other", remote="example.invalid/acme/other")
    # Added later: applied at the next sighting, a logged row and a line.
    _flag(app.path, True)
    fenced = scene.see(app)
    assert any("now declares a boundary" in line for line in fenced.lines)
    project = scene.model.project(root.project)
    assert project.boundary and project.tenant == root.project
    assert fenced.tenant == root.project
    assert any(
        row["project"]["id"] == root.project and row["project"]["boundary"] == 1
        for row in scene.rows("project_put")
    )
    assert _sees(scene.see(other), _home(scene, at_root)) == []
    # Removed: the boundary stays, and the line names what lifts it.
    _flag(app.path, False)
    still = scene.see(app)
    assert any(
        "no longer declares a boundary" in line
        and f"bettermemory project init --rebind (in {app.path})" in line
        for line in still.lines
    )
    assert scene.model.project(root.project).boundary
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "init", "--rebind")
    out = capsys.readouterr().out
    assert "and lifts the boundary of" in out and "no longer a boundary" in out
    lifted = scene.model.project(root.project)
    assert not lifted.boundary and lifted.tenant is None
    after = scene.see(app)
    assert after.tenant is None
    assert not any("boundary" in line for line in after.lines)
    assert SEARCH_UNSCOPED in _sees(scene.see(other), _home(scene, at_root))


def test_b12_a_parts_boundary_follows_its_file_and_rebind_keeps_the_part(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono = scene.repo("mono", remote=URL)
    root = scene.see(mono)
    at_root = scene.write(root)
    vault_dir = mono.path / "packages" / "vault"
    _init_at(scene, monkeypatch, capsys, mono, vault_dir)
    part = scene.see(mono)
    assert part.project is not None
    in_vault = scene.write(part)
    _flag(vault_dir, True)
    fenced = scene.see(mono)
    assert any("now declares a boundary" in line for line in fenced.lines)
    assert fenced.tenant == part.project
    assert _sees(fenced, _home(scene, at_root)) == []
    _flag(vault_dir, False)
    still = scene.see(mono)
    assert any(
        f"bettermemory project init --rebind (in {vault_dir})" in line
        for line in still.lines
    )
    typed(monkeypatch, "vault")
    bm(monkeypatch, "project", "init", "--rebind")
    out = capsys.readouterr().out
    assert f'bound to project "vault" ({part.project})' in out
    lifted = scene.model.project(part.project)
    assert not lifted.boundary and lifted.tenant is None
    assert lifted.parent == root.project
    res = scene.see(mono)
    assert (res.outcome, res.project, res.tenant) == (SILENT, part.project, None)
    assert SESSION_BRIEF in _sees(res, _home(scene, at_root))
    mono.at = None
    assert SEARCH_AUTO_SCOPE in _sees(scene.see(mono), _home(scene, in_vault))


def test_x06_e35_a_nested_file_of_another_repository_answered_both_ways(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mono = scene.repo("mono")
    project_file.write(mono.path, name="mono")
    root = scene.see(mono)
    other = scene.repo("other", remote="example.invalid/acme/other")
    project_file.write(other.path, name="other")
    theirs = scene.see(other)
    copied = (other.path / project_file.PROJECT_FILE_NAME).read_bytes()
    for name in ("tools", "tools2"):
        (mono.path / "packages" / name).mkdir(parents=True)
        (mono.path / "packages" / name / project_file.PROJECT_FILE_NAME).write_bytes(
            copied
        )
    # (e, f) asked, then answered join: that folder is the other project.
    mono.at = mono.path / "packages" / "tools"
    asked = scene.see(mono)
    assert (asked.outcome, asked.project) == (ASK, root.project)
    assert "bettermemory project init --new (in that folder)" in asked.lines[-1]
    typed(monkeypatch, "other")
    bm(monkeypatch, "project", "join", str(theirs.project))
    assert scene.see(mono).project == theirs.project
    # (g) answered init --new: a sub-project of the monorepo, its own id.
    mono.at = mono.path / "packages" / "tools2"
    again = scene.see(mono)
    assert again.outcome == ASK
    capsys.readouterr()
    typed(monkeypatch, "tools2")
    bm(monkeypatch, "project", "init", "--new")
    out = capsys.readouterr().out
    assert "replaces" in out and "answers question" in out
    assert (mono.at / project_file.PROJECT_FILE_NAME).read_bytes() != copied
    res = scene.see(mono)
    assert res.outcome == SILENT and res.project not in (root.project, theirs.project)
    assert scene.model.chain_of(res.project) == [res.project, root.project]
    assert scene.model.pending() == []


# ---------------------------------------------------------------------------
# memory move
# ---------------------------------------------------------------------------


def test_memory_move_rehomes_one_record(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    a = scene.see(scene.repo("a"))
    b = scene.see(scene.repo("b"))
    rid = scene.write(a)
    other = scene.write(a)
    typed(monkeypatch, "b")
    bm(monkeypatch, "memory", "move", rid, "--project", str(b.project))
    out = capsys.readouterr().out
    assert f'memory {rid} (written in checkout {a.checkout}) leaves "a"' in out
    assert out.rstrip().endswith(f'{rid} is in "b" ({b.project})')
    home = scene.model.home(rid)
    assert home is not None and home.project == b.project
    assert home.rule.endswith("|declaration:os:tester:move")
    kept = scene.model.home(other)
    assert kept is not None and kept.project == a.project
    typed(monkeypatch, "global")
    bm(monkeypatch, "memory", "move", rid, "--project", "global")
    home = scene.model.home(rid)
    assert home is not None and home.project == GLOBAL
    typed(monkeypatch, "b")
    err = refused(
        monkeypatch, capsys, "memory", "move", generate_ulid(), "--project", "global"
    )
    assert "has no home recorded" in err


# ---------------------------------------------------------------------------
# Shallow clones: the answer by evidence and the declared remote (D18)
# ---------------------------------------------------------------------------


def _shallow(scene: Scene, source: Repo, name: str) -> Repo:
    """A shallow clone at a tip no witness names: its history holds the
    tip only."""
    tip = source.commit()
    clone = scene.repo(name, commits=0)
    clone.history = History({tip: ()})
    clone.head = tip
    clone.remotes = set(source.remotes)
    clone.shallow = True
    return clone


def test_i24_the_answer_by_evidence_closes_the_question(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = scene.repo("work", remote=URL)
    project = scene.see(work)
    ci = _shallow(scene, work, "ci")
    asked = scene.see(ci)
    assert (asked.outcome, asked.rule) == (ASK, "ask:one_witness")
    bm(monkeypatch, "project", "pending")
    assert str(asked.proposal) in capsys.readouterr().out
    # `git fetch --unshallow`: the clone now holds the witness commits.
    ci.history = work.history.copy()
    ci.shallow = False
    closed = scene.see(ci)
    assert (closed.outcome, closed.project) == (SILENT, project.project)
    assert any("what was written here joined it" in line for line in closed.lines)
    pr = scene.model.proposal(str(asked.proposal))
    assert (pr.state, pr.answered_by) == (mb.ANSWERED, "evidence:H+R")
    bm(monkeypatch, "project", "pending")
    assert capsys.readouterr().out == "bettermemory: no open questions\n"


def test_t13_d18_join_in_a_shallow_clone_declares_the_remote(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = scene.repo("work", remote=URL)
    project = scene.see(work)
    first = _shallow(scene, work, "run1")
    asked = scene.see(first)
    assert asked.outcome == ASK
    capsys.readouterr()
    typed(monkeypatch, "work")
    bm(monkeypatch, "project", "join", str(project.project))
    out = capsys.readouterr().out
    assert f'{URL} is declared "work" ({project.project})\'s remote' in out
    declared = scene.store.membership_rows("declared_remotes")
    assert declared == [
        {"remote_key": URL, "project": project.project, "principal": "os:tester"}
    ]
    second = _shallow(scene, work, "run2")
    res = scene.see(second)
    assert (res.outcome, res.project, res.rule) == (SILENT, project.project, "join:D+R")
    # Where history speaks, the declared remote does not (E32 f).
    other = scene.repo("other", remote=URL)
    assert scene.see(other).outcome == ASK


# ---------------------------------------------------------------------------
# membership migrate (design 8.6)
# ---------------------------------------------------------------------------


def _unhomed_store(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A store at `BETTERMEMORY_DIR` whose records carry no home, as an
    upgraded store's do: two memories and an episode written in a live
    repository, one memory in a folder that is gone. Returns the
    repository and the gone folder."""
    monkeypatch.setenv("BETTERMEMORY_DIR", str(root / "store"))
    repo, gone = root / "work" / "app", root / "old" / "gone"
    repo.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("commit", "-q", "--allow-empty", "-m", "first"),
    ):
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    store = Store(load_config().resolved_directory())
    try:
        for at in (repo, repo, gone):
            store.put_memory(
                Memory(
                    id=generate_ulid(),
                    created=_NOW,
                    updated=_NOW,
                    scopes=["tools"],
                    confidence=Confidence.MEDIUM,
                    source=Source.EXPLICIT,
                    body=f"memory at {at.name}\n",
                    origin=Origin(cwd=str(at), worktree_root=str(at)),
                )
            )
        store.write_episode(
            session_id="s1",
            body="episode",
            origin=Origin(cwd=str(repo), worktree_root=str(repo)),
            now=_NOW,
        )
    finally:
        store.close()
    return repo, gone


def _rows(*tables: str) -> dict[str, list[str]]:
    """Every row of the CLI store's `tables`, as text."""
    store = Store(load_config().resolved_directory())
    try:
        return {
            t: sorted(repr(tuple(r)) for r in store.conn.execute(f"SELECT * FROM {t}"))
            for t in tables
        }
    finally:
        store.close()


_RECORDS = ("memories", "episodes")
_MEMBERSHIP = ("log", "homes", "projects", "checkouts", "checkout_views", "proposals")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_membership_migrate_dry_run_then_run(
    tmp_path: Path,
    neutral_git: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`bettermemory membership migrate --dry-run` prints the report and
    writes nothing; `bettermemory membership migrate` homes every record
    once, by the migration's rules, rewriting no record row and writing
    nothing into the repository; a second run homes nothing."""
    root = tmp_path.resolve()
    repo, gone = _unhomed_store(root, monkeypatch)
    records = _rows(*_RECORDS)
    before = _rows(*_MEMBERSHIP)
    assert before["homes"] == []
    git_files = sorted(str(p) for p in (repo / ".git").rglob("*"))
    bm(monkeypatch, "membership", "migrate", "--dry-run")
    captured = capsys.readouterr()
    assert captured.out.startswith("bettermemory membership migrate (dry run: ")
    assert f"\n  {repo}: live, " in captured.out
    assert f"\n  {gone}: gone, " in captured.out
    assert "3 memory homes, 0 removed-memory homes, 1 episode homes" in captured.out
    assert captured.err == ""
    assert _rows(*_MEMBERSHIP) == before
    bm(monkeypatch, "membership", "migrate")
    captured = capsys.readouterr()
    assert captured.out.startswith("bettermemory membership migrate: ")
    assert "3 memory homes, 0 removed-memory homes, 1 episode homes" in captured.out
    assert captured.err == ""
    after = _rows(*_MEMBERSHIP)
    assert _rows(*_RECORDS) == records
    store = Store(load_config().resolved_directory())
    try:
        homes = store.membership_rows("homes")
        ids = {str(r[0]) for r in store.conn.execute("SELECT id FROM memories")}
        ids |= {str(r[0]) for r in store.conn.execute("SELECT id FROM episodes")}
        assert sorted(str(h["record_id"]) for h in homes) == sorted(ids)
        assert all(str(h["rule"]).startswith("migration:") for h in homes)
        assert len(list(store.membership_log(["membership_migrated"]))) == 1
    finally:
        store.close()
    assert sorted(str(p) for p in (repo / ".git").rglob("*")) == git_files
    assert checkout_token.read(repo / ".git").token is None
    bm(monkeypatch, "membership", "migrate")
    captured = capsys.readouterr()
    assert captured.out.startswith("bettermemory membership migrate: nothing to home")
    assert _rows(*_MEMBERSHIP) == after


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_command_migrates_an_unhomed_store_at_open(
    tmp_path: Path,
    neutral_git: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Design 8.6, the run at open: the first command over a store with a
    record homed nowhere homes every record before it runs and says so in
    one stderr line; the next command finds nothing to home."""
    root = tmp_path.resolve()
    repo, _gone = _unhomed_store(root, monkeypatch)
    bm(monkeypatch, "project", "list")
    captured = capsys.readouterr()
    assert captured.err == (
        "bettermemory: membership migrated: 4 records homed, "
        "0 legacy questions opened\n"
    )
    assert '"app"' in captured.out and f"live at {repo}" in captured.out
    homed = _rows("homes", "log")
    assert len(homed["homes"]) == 4
    bm(monkeypatch, "project", "list")
    again = capsys.readouterr()
    assert (again.out, again.err) == (captured.out, "")
    assert _rows("homes", "log") == homed


@pytest.mark.parametrize("command", ["project", "memory", "membership"])
def test_a_bare_command_prints_its_help_and_exits_2(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    with pytest.raises(SystemExit) as exc:
        bm(monkeypatch, command)
    assert exc.value.code == 2
    assert f"usage: bettermemory {command}" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The installer removes the deny rules an earlier candidate added
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME at a scratch directory: the installer never reaches the real
    one."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.setattr("bettermemory.init.find_binary", lambda: "/fake/bm")
    assert Path.home() == fake
    return fake


def test_installer_removes_the_deny_rules_once_and_keeps_every_other(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from bettermemory.init import DECLARATION_DENY_RULES

    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "permissions": {
                    "allow": ["Bash(ls:*)"],
                    "deny": ["Read(./.env)", *DECLARATION_DENY_RULES],
                },
            }
        ),
        encoding="utf-8",
    )
    bm(monkeypatch, "init", "--client", "claude-code")
    out = capsys.readouterr().out
    assert (
        f"removed from {settings} the deny rules an earlier candidate added: "
        + ", ".join(DECLARATION_DENY_RULES)
        in out
    )
    assert "nothing is left for you to run" in out
    body = json.loads(settings.read_text(encoding="utf-8"))
    assert body["model"] == "opus"
    assert body["permissions"]["allow"] == ["Bash(ls:*)"]
    assert body["permissions"]["deny"] == ["Read(./.env)"]
    assert (
        json.loads((home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"][
            "bettermemory"
        ]["command"]
        == "/fake/bm"
    )
    # A second run finds nothing to remove and says nothing about it.
    bm(monkeypatch, "init", "--client", "claude-code")
    out = capsys.readouterr().out
    assert "deny rules" not in out
    assert json.loads(settings.read_text(encoding="utf-8")) == body


def test_installer_creates_no_settings_file_and_reports_noop_in_json(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bm(monkeypatch, "init", "--client", "claude-code", "--json")
    payload = json.loads(capsys.readouterr().out)
    settings = home / ".claude" / "settings.json"
    assert payload["permissions"] == {
        "action": "noop",
        "path": str(settings),
        "removed": [],
    }
    assert not settings.exists()
    assert not (home / ".claude").exists()


def test_installer_project_scope_and_print_only(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    bm(
        monkeypatch,
        "init",
        "--client",
        "claude-code",
        "--config-path",
        str(project / ".mcp.json"),
        "--print-only",
    )
    out = capsys.readouterr().out
    assert "permissions.deny" not in out and "terminal" not in out
    assert not (project / ".claude").exists()
    bm(
        monkeypatch,
        "init",
        "--client",
        "claude-code",
        "--config-path",
        str(project / ".mcp.json"),
    )
    assert (project / ".mcp.json").is_file()
    assert not (project / ".claude").exists()
    assert not (home / ".claude").exists()


def test_installer_leaves_a_malformed_deny_list_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    original = json.dumps({"permissions": {"deny": "Bash(rm:*)"}})
    settings.write_text(original, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        bm(monkeypatch, "init", "--client", "claude-code")
    assert exc.value.code == 2
    assert "is not a list" in capsys.readouterr().err
    assert settings.read_text(encoding="utf-8") == original


def test_other_clients_get_no_settings_file(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cursor.json"
    bm(monkeypatch, "init", "--client", "cursor", "--config-path", str(target))
    assert not (tmp_path / ".claude").exists()
    assert not (home / ".claude").exists()


__all__ = [
    "History",
    "Repo",
    "Scene",
    "bm",
    "file_ref",
    "folder_location",
    "refused",
    "typed",
]


# ---------------------------------------------------------------------------
# End to end: the real `location.resolve_here` on a real repository
# ---------------------------------------------------------------------------


@pytest.fixture
def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git with no global or system configuration, no hooks and a fixed
    author, as experiments/lab.py runs it."""
    empty = tmp_path / "gitconfig-empty"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "Fixture")
        monkeypatch.setenv(f"{var}_EMAIL", "fixture@example.invalid")
        monkeypatch.setenv(f"{var}_DATE", "2026-01-02T03:04:05Z")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "BETTERMEMORY_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_init_and_new_through_the_real_resolution(
    tmp_path: Path,
    neutral_git: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("bettermemory.cli.project_cmd.principal", lambda: "os:tester")
    root = tmp_path.resolve()
    monkeypatch.setenv("BETTERMEMORY_DIR", str(root / "store"))
    repo = root / "work" / "app"
    repo.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (repo / "README").write_text("app\n", encoding="utf-8")
    git("add", "README")
    git("commit", "-q", "-m", "first")
    monkeypatch.chdir(repo)
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "init")
    out = capsys.readouterr().out
    written = project_file.parse(repo / project_file.PROJECT_FILE_NAME)
    assert isinstance(written, project_file.ProjectFile)
    assert f"bettermemory: wrote {repo / project_file.PROJECT_FILE_NAME}" in out
    bm(monkeypatch, "project", "list")
    listing = capsys.readouterr().out
    assert '"app"' in listing and f"live at {repo}" in listing
    token = checkout_token.read(repo / ".git").token
    assert token is not None
    # The root file is bound: `new` would leave it naming the project the
    # checkout leaves, so it refuses and names `init --new` (B11).
    typed(monkeypatch, "app")
    err = refused(monkeypatch, capsys, "project", "new")
    assert "bettermemory project init --new" in err
    typed(monkeypatch, "app")
    bm(monkeypatch, "project", "init", "--new")
    out = capsys.readouterr().out
    assert "a project of its own" in out
    assert "its checkout token was re-minted" in out
    assert checkout_token.read(repo / ".git").token not in (None, token)
    # Below the root (D13): a sub-project of the checkout's project, which a
    # session in that folder then resolves to through the real capture.
    web = repo / "packages" / "web"
    web.mkdir(parents=True)
    monkeypatch.chdir(web)
    typed(monkeypatch, "web")
    bm(monkeypatch, "project", "init")
    out = capsys.readouterr().out
    assert f'and makes {web} a sub-project "web" of "app"' in out
    location.forget()
    store = Store(load_config().resolved_directory())
    try:
        at_root = location.resolve_here(store, load_config(), str(repo))
        res = location.resolve_here(store, load_config(), str(web))
        assert res.outcome == SILENT and res.checkout == at_root.checkout
        assert res.chain[:2] == (res.project, at_root.project)
        assert (
            Membership(store, location.process_machine().id)
            .project(str(res.project))
            .name
            == "web"
        )
    finally:
        store.close()

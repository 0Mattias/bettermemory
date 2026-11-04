"""The migration homes a record written in a part's or a sub-project's
folder where a session there resolves (ENG-212).

Rule D4 (`_Run._recorded_root_of`, ``cwd_in_root``) homes a record with no
recorded root whose cwd lies inside a recorded root, or a view of this
machine's checkouts, to that root's project. It ran before the ladder, so a
record written in the folder of a part declared below a folder project's
root, or below a sub-project's file inside a repository, went to the
enclosing project, while a session in that folder resolves to the part or
the sub-project. The ladder now resolves such a cwd (`_Run._finer_at`) and
its project wins where it is a part or a sub-project of D4's; a record in
the enclosing project's own folders still goes there by D4.

The three shapes are the ENG-204 builder's probes: a part declared from
9.0.0rc14 on (its folder stored as a view records it) with the record at
that folder; a part whose stored folder is now a link's spelling, the
record in that spelling; a repository with a recorded root and a
sub-project's file below it. Each test also writes a record in the
enclosing project's own folder, and reads the live resolution at the
part's folder after the run. Without birth time
(``BETTERMEMORY_TEST_NO_BIRTH=1``) the rules are those the ladder gives
there.

The gate (`_Run._finer_files`) is held to the ladder's own reading: a
folder project first seen below the root, one or two levels down and in
either record order; a linked worktree inside the root's working tree,
which the ladder finds by its object store, and a checkout whose row has
no object store key, found from its worktree by its git directory; a
folder project `git init` turned into a repository, found by its working
tree's root (``rekeyed:F+O``); a checkout of another machine, never the
cwd's repository; a sub-project file a session bound already, and a
nested file bound to the root's own project, which leave the gate open;
a file nested in the root's repository bound outside its project, which
would open a question on the root's checkout, left to D4; a repository
of its own below a folder root or a repository root, no part, so the run
resolves nothing there and registers nothing; the root's own folders,
never resolved. And a copy of a part's file, which the ladder asks about:
every record at that folder, and one below it, keeps D4, with the ladder
asked once; what the question leaves is counted, and a later run's record
there goes to the provisional project through the view it left. A part
declared below that copy is still found, in the same run and in a later
one, after an earlier run or a session asked at the copy, and after the
question was answered either way (ENG-222), as is a part declared inside
another part's folder; and a moved
copy's records keep D4 while one at its old path keeps the asked
checkout, whose views the run does not pass over.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

import pytest

from bettermemory import location, project_file
from bettermemory import membership_migrate as mm
from bettermemory.config import Config
from bettermemory.location import resolve_caller
from bettermemory.membership import Declaration, Membership, Proposal, Resolution
from bettermemory.membership_migrate import MigrationReport, migrate
from bettermemory.origin import Origin
from bettermemory.store import Store

from .test_membership_hung_view import _NOW, _folder_with_a_part, lab
from .test_membership_resolution import birth_reported, known_rule
from .test_membership_security import _git, checkout

__all__ = ["lab"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


def _rootless(store: Store, cwd: Path | str, body: str) -> str:
    """An episode written at `cwd` with no recorded root and no remote, as a
    build before the membership tables wrote it; its id."""
    return store.write_episode(
        session_id="s1", body=body, origin=Origin(cwd=str(cwd)), now=_NOW
    ).id


def _homes(report: MigrationReport) -> dict[str, tuple[str, str | None, str]]:
    """Each rootless record's project, checkout and rule, by record id."""
    return {h.record: (h.project, h.checkout, h.rule) for h in report.rootless}


def _live(store: Store, config: Config, cwd: Path | str) -> tuple[str | None, str]:
    """A session's resolution at `cwd`: its project and rule."""
    location.forget()
    res = resolve_caller(store, config, str(cwd)).resolution
    return res.project, res.rule


def _rooted(store: Store, root: Path) -> None:
    """An episode whose origin names `root` as its root: the migration
    probes it as a recorded root (`_live_root`)."""
    store.write_episode(
        session_id="s1",
        body="at the root",
        origin=Origin(cwd=str(root), worktree_root=str(root)),
        now=_NOW,
    )


def _writing(tmp_path: Path, store: Store, config: Config) -> tuple[Path, Resolution]:
    """`Writing`, a folder project a session resolved, and that resolution."""
    outer = Path(os.path.realpath(tmp_path)) / "Writing"
    outer.mkdir(parents=True)
    project_file.write(outer, name="writing")
    known = resolve_caller(store, config, str(outer)).resolution
    assert known.checkout is not None and known.project is not None
    return outer, known


def _pending(store: Store) -> list[Proposal]:
    """The open questions, as `bettermemory project pending` lists them."""
    return Membership(store, location.process_machine().id).pending()


def _resolutions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The cwds a migration run asks the ladder about from here on, as it
    asks (`_Run._resolved_at`), in order."""
    resolved: list[str] = []
    real = mm._Run._resolved_at

    def counted(run: mm._Run, cwd: str) -> object:
        resolved.append(cwd)
        return real(run, cwd)

    monkeypatch.setattr(mm._Run, "_resolved_at", counted)
    return resolved


def _declare(store: Store, known: Resolution, folder: Path, name: str) -> str:
    """A part of `known`'s project declared in `folder`, a folder below its
    root (made where it is not there yet), as `project init` there declares
    it (B17, I31), from 9.0.0rc14 on: its folder stored as a view records
    it. The part."""
    folder.mkdir(exist_ok=True)
    model = Membership(store, location.process_machine().id)
    written = project_file.write(folder, name=name)
    location.forget()
    model.declare(
        Declaration(
            "init",
            "os:tester",
            checkout=known.checkout,
            target=known.project,
            file_id=written.id,
            name=name,
            directory=str(folder),
        )
    )
    part = model.file_binding(written.id)
    assert part is not None and model.chain_of(part) == [part, known.project]
    return part


def _declared_part(
    tmp_path: Path, store: Store, config: Config
) -> tuple[Path, Path, Resolution, str]:
    """`Writing` (`_writing`) and `Writing/novel`, a part of it
    (`_declare`). The two folders, the enclosing project's resolution, and
    the part."""
    outer, known = _writing(tmp_path, store, config)
    inner = outer / "novel"
    return outer, inner, known, _declare(store, known, inner, "novel")


def test_a_record_in_a_part_declared_from_rc14_on_is_homed_to_the_part(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A part declared below a folder project's root, its folder stored as a
    view records it: a record at the part's folder, which lies inside the
    enclosing project's view, joins the part by the declaration's rung, as a
    session there does; one in the enclosing folder's own subfolder stays
    with the enclosing project (D4)."""
    store, config = lab
    outer, inner, known, part = _declared_part(tmp_path, store, config)
    (outer / "drafts").mkdir()
    in_part = _rootless(store, inner, "in the part")
    in_outer = _rootless(store, outer / "drafts", "in the enclosing folder")

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    project, at, rule = homes[in_part]
    assert (project, rule) == (part, "migration:cwd:declared_folder")
    assert at is not None and at != known.checkout
    assert homes[in_outer] == (known.project, known.checkout, "migration:cwd_in_root")
    assert _live(store, config, inner)[0] == part


def test_a_record_at_a_parts_link_spelling_is_homed_to_the_part(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A part whose tree moved behind a link at its old name, so its stored
    folder and the enclosing project's view are spelled through the link: a
    record at the part's folder in that spelling (inside the view) joins the
    part's checkout, which a record at its real folder registered earlier in
    the run, by its object, as a session at that spelling does; one at the
    enclosing folder in that spelling stays with the enclosing project."""
    store, config = lab
    here, stored, part = _folder_with_a_part(tmp_path, store, config)
    model = Membership(store, location.process_machine().id)
    enclosing = model.project(part).parent
    (outer,) = [k for k in model.checkouts() if k.project == enclosing]
    at_real = _rootless(store, here, "at the real folder")
    at_link = _rootless(store, stored, "at the link spelling")
    in_outer = _rootless(store, Path(stored).parent, "in the enclosing folder")

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    assert homes[at_real][::2] == (part, "migration:cwd:declared_folder")
    assert homes[at_link] == (part, homes[at_real][1], "migration:cwd:folder_object")
    assert homes[in_outer] == (enclosing, outer.id, "migration:cwd_in_root")
    assert _live(store, config, stored) == (part, "folder_object")


def test_a_legacy_record_in_a_sub_project_folder_is_homed_to_the_sub_project(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A repository with a recorded root and a sub-project's file below it
    (D13): a record with no root at the sub-project's folder is homed to the
    sub-project the ladder binds that file to, in the repository's checkout,
    as a session there resolves; one in another folder of the repository
    stays with the repository's project (D4)."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    sub = repo / "pkg"
    sub.mkdir()
    written = project_file.write(sub, name="pkg")
    _rooted(store, repo)
    in_sub = _rootless(store, sub, "in the sub-project")
    in_repo = _rootless(store, repo / "src", "in the repository")

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    (root,) = report.roots
    homes = _homes(report)
    project, at, rule = homes[in_sub]
    assert (at, rule) == (
        root.checkout,
        "migration:cwd:" + known_rule(repo, without_birth="last_head"),
    )
    model = Membership(store, location.process_machine().id)
    assert project == model.file_binding(written.id)
    assert model.project(project).parent == root.project
    assert homes[in_repo] == (root.project, root.checkout, "migration:cwd_in_root")
    assert _live(store, config, sub)[0] == project


def test_a_folder_project_first_seen_below_the_root_is_homed_there(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A folder below a folder project's root holding a project file no
    project is bound to yet: the ladder makes it a folder project inside
    the enclosing one, as a session's first sight there does, and the
    record goes to it."""
    store, config = lab
    outer, known = _writing(tmp_path, store, config)
    essay = outer / "essay"
    essay.mkdir()
    project_file.write(essay, name="essay")
    in_essay = _rootless(store, essay, "in the essay")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    project, _at, rule = _homes(report)[in_essay]
    assert rule == "migration:cwd:new_folder"
    model = Membership(store, location.process_machine().id)
    assert model.chain_of(project) == [project, known.project]
    assert _live(store, config, essay)[0] == project


@pytest.mark.parametrize("first", ["outer", "inner"])
def test_folder_projects_two_levels_below_the_root_home_in_either_order(
    tmp_path: Path, lab: tuple[Store, Config], first: str
) -> None:
    """`Writing/a` and `Writing/a/b` each hold a project file no project is
    bound to yet, a record in each. A session at `a/b` makes `a` a folder
    project inside `Writing` and `b` one inside `a`. Each record goes where
    a session in its folder resolves, whichever comes first: the nearest
    enclosing file bound to a project decides, not the nearest file."""
    store, config = lab
    outer, known = _writing(tmp_path, store, config)
    a = outer / "a"
    b = a / "b"
    b.mkdir(parents=True)
    project_file.write(a, name="a")
    project_file.write(b, name="b")
    order = [a, b] if first == "outer" else [b, a]
    ids = {p.name: _rootless(store, p, f"in {p.name}") for p in order}
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    in_a, in_b = homes[ids["a"]][0], homes[ids["b"]][0]
    model = Membership(store, location.process_machine().id)
    assert model.chain_of(in_b) == [in_b, in_a, known.project]
    assert (_live(store, config, a)[0], _live(store, config, b)[0]) == (in_a, in_b)


def test_a_record_in_a_linked_worktree_inside_the_root_joins_the_sub_project(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A linked worktree inside the recorded main worktree (the
    ``.claude/worktrees/<name>`` layout), with a committed sub-project file
    no session has bound yet: the ladder finds the worktree by its object
    store, the repository's (S), and the sub-project below it, so a record
    at the worktree's sub-project folder goes there, as a session does."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    (repo / "pkg").mkdir()
    project_file.write(repo / "pkg", name="pkg")
    (repo / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pkg")
    tree = repo / ".claude" / "worktrees" / "feature"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(tree))
    _rooted(store, repo)
    in_tree = _rootless(store, tree / "pkg", "in the worktree's pkg")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    (root,) = report.roots
    project, at, rule = _homes(report)[in_tree]
    assert rule.startswith("migration:cwd:common_object"), (project, rule)
    assert at not in (None, root.checkout)
    model = Membership(store, location.process_machine().id)
    assert model.project(project).parent == root.project
    assert _live(store, config, tree / "pkg")[0] == project


def test_a_nested_file_bound_outside_the_root_opens_no_question(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A repository with a recorded root; `vendor` in it holds a copy of
    another project's file, and `vendor/sub` a file bound to none. The
    ladder would ask about `vendor` on the repository's own checkout, which
    every session in the repository is then asked, and place the record
    nowhere finer: the run resolves nothing there, opens no question, and
    the record stays with the repository's project (D4)."""
    store, config = lab
    other = Path(os.path.realpath(tmp_path)) / "elsewhere" / "x"
    other.mkdir(parents=True)
    project_file.write(other, name="x")
    resolve_caller(store, config, str(other))
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    sub = repo / "vendor" / "sub"
    sub.mkdir(parents=True)
    shutil.copyfile(
        other / project_file.PROJECT_FILE_NAME,
        repo / "vendor" / project_file.PROJECT_FILE_NAME,
    )
    project_file.write(sub, name="sub")
    _rooted(store, repo)
    in_sub = _rootless(store, sub, "in vendor/sub")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    (root,) = report.roots
    assert _homes(report)[in_sub] == (
        root.project,
        root.checkout,
        "migration:cwd_in_root",
    )
    assert report.totals.proposals == 0
    assert _pending(store) == []


def test_a_folder_whose_file_names_another_project_opens_no_question(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A folder below a folder project's root holding a copy of another,
    unrelated project's file: no part of the root's project is found there,
    so the run resolves nothing (a session's first sight would ask about
    it), mints nothing, and the record stays with the root's project
    (D4)."""
    store, config = lab
    other = Path(os.path.realpath(tmp_path)) / "Other"
    other.mkdir()
    project_file.write(other, name="other")
    resolve_caller(store, config, str(other))
    outer, known = _writing(tmp_path, store, config)
    copied = outer / "copied"
    copied.mkdir()
    shutil.copyfile(
        other / project_file.PROJECT_FILE_NAME, copied / project_file.PROJECT_FILE_NAME
    )
    in_copied = _rootless(store, copied, "in the copied folder")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    assert _homes(report)[in_copied] == (
        known.project,
        known.checkout,
        "migration:cwd_in_root",
    )
    t = report.totals
    assert (t.projects, t.checkouts, t.proposals) == (0, 0, 0)


@pytest.mark.parametrize("below", ["folder", "repository"])
def test_a_repository_of_its_own_below_a_root_keeps_d4_and_registers_nothing(
    tmp_path: Path, lab: tuple[Store, Config], below: str
) -> None:
    """A repository of its own below a folder project's root (with its own
    project file, bound to none) or below a repository's recorded root
    (with a sub-project's file nested in it, bound to none): the ladder
    would make it a project of its own, no part of the root's, so a record
    with no root there stays with the root's project (D4) and the run
    resolves nothing there: it registers no checkout and mints no project
    beyond the root's own."""
    store, config = lab
    if below == "folder":
        outer, known = _writing(tmp_path, store, config)
        inner = checkout(outer / "tool")
        project_file.write(inner, name="tool")
        cwd, minted = inner, (0, 0)
    else:
        repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
        _rooted(store, repo)
        inner = checkout(repo / "inner")
        (inner / "pkg").mkdir()
        project_file.write(inner / "pkg", name="pkg")
        cwd, minted = inner / "pkg", (1, 1)
    in_inner = _rootless(store, cwd, "in the inner repository")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    if below == "folder":
        home = (known.project, known.checkout)
    else:
        (root,) = report.roots
        home = (root.project, root.checkout)
    assert _homes(report)[in_inner] == (*home, "migration:cwd_in_root")
    assert (report.totals.projects, report.totals.checkouts) == minted


def test_records_in_a_roots_own_folders_resolve_nothing(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records in a folder root's and a repository root's own folders, at
    the root and below it: each goes to the root's project by D4, and the
    run resolves no cwd at all."""
    store, config = lab
    outer, _known = _writing(tmp_path, store, config)
    (outer / "drafts" / "deep").mkdir(parents=True)
    repo = checkout(tmp_path / "work" / "app", files={"src/x/main.txt": "main\n"})
    _rooted(store, repo)
    ids = [
        _rootless(store, cwd, "in the root's own folder")
        for cwd in (
            outer,
            outer / "drafts",
            outer / "drafts" / "deep",
            repo / "src",
            repo / "src" / "x",
        )
    ]
    resolved = _resolutions(monkeypatch)
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    assert [homes[i][2] for i in ids] == ["migration:cwd_in_root"] * len(ids)
    assert resolved == []


def _copy_of_a_part(
    tmp_path: Path, store: Store, config: Config
) -> tuple[Path, Resolution, str]:
    """`Writing` with its part `novel` (`_declared_part`), and
    `Writing/copy`, a folder holding a copy of the part's project file and
    other content. The copy's folder, the enclosing project's resolution and
    the part."""
    outer, inner, known, part = _declared_part(tmp_path, store, config)
    copy = outer / "copy"
    copy.mkdir()
    (copy / "other.md").write_text("other\n", encoding="utf-8")
    shutil.copyfile(
        inner / project_file.PROJECT_FILE_NAME, copy / project_file.PROJECT_FILE_NAME
    )
    return copy, known, part


def test_every_record_at_a_folder_the_ladder_asks_about_keeps_d4(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder below the root holding a copy of a part's project file and
    other content: the file names a part, so the ladder runs there for the
    first record, but it asks (the file alone, a project of its own until
    answered), which is no part of the enclosing project, so the record
    stays with the enclosing project by D4. What the question leaves is a
    session's first sight there: a provisional project, a checkout with a
    view at the copy's folder, and one open question about that checkout
    (``one_witness``). The next record at the copy, and one in a folder
    below it, find that view as their root; they keep the enclosing project
    too, and the ladder is not asked again. A later run finds the view the
    first one left, and a record written at the copy since goes to the
    provisional project the question is about, by D4, as a session there
    joins it, with birth time and without (ENG-219)."""
    store, config = lab
    copy, known, part = _copy_of_a_part(tmp_path, store, config)
    (copy / "notes").mkdir()
    ids = [
        _rootless(store, copy, "in the copy"),
        _rootless(store, copy, "in the copy again"),
        _rootless(store, copy / "notes", "below the copy"),
    ]
    resolved = _resolutions(monkeypatch)
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    d4 = (known.project, known.checkout, "migration:cwd_in_root")
    assert [homes[i] for i in ids] == [d4] * 3
    assert resolved == [str(copy)]
    t = report.totals
    assert (t.projects, t.checkouts, t.proposals) == (1, 1, 1)
    (question,) = _pending(store)
    assert (question.reason, question.candidates) == ("one_witness", (part,))
    model = Membership(store, location.process_machine().id)
    assert question.checkout is not None and question.checkout != known.checkout
    asked = model.checkout(question.checkout)
    assert asked.newest_view is not None and asked.newest_view.path == str(copy)
    assert model.project(asked.project).provisional is not None

    later = _rootless(store, copy, "in the copy after the run")
    location.forget()

    again = migrate(store, location.process_machine(), now=lambda: _NOW)

    assert _homes(again)[later] == (asked.project, asked.id, "migration:cwd_in_root")
    assert resolved == [str(copy)]
    t = again.totals
    assert (t.projects, t.checkouts, t.proposals) == (0, 0, 0)
    assert len(_pending(store)) == 1


def test_a_part_declared_below_a_folder_the_ladder_asked_about_is_found(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """The copy of a part's file (`_copy_of_a_part`), and below it a part
    of the enclosing project declared in a folder of its own: a record at
    the copy registers the view the ladder's question leaves, and a later
    record at the declared part's folder, which lies in that view, is still
    homed to the part, as a session there resolves: the view keeps the
    enclosing project's home for the location the ladder asked about, and
    the gate reads the part's own file against that home."""
    store, config = lab
    copy, known, _part = _copy_of_a_part(tmp_path, store, config)
    below = copy / "essay"
    essay = _declare(store, known, below, "essay")
    in_copy = _rootless(store, copy, "in the copy")
    in_essay = _rootless(store, below, "in the part below the copy")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    assert homes[in_copy] == (known.project, known.checkout, "migration:cwd_in_root")
    assert homes[in_essay][::2] == (essay, "migration:cwd:declared_folder")
    assert _live(store, config, below)[0] == essay


@pytest.mark.parametrize("records", ["deep", "essay,deep"])
@pytest.mark.parametrize("asked_by", ["earlier run", "session"])
def test_a_part_below_a_copy_asked_about_before_the_run_keeps_its_records(
    tmp_path: Path, lab: tuple[Store, Config], asked_by: str, records: str
) -> None:
    """The copy of a part's file (`_copy_of_a_part`), asked about before
    this run, by an earlier run's record at the copy or by a session there:
    the copy's provisional project, its checkout's view at the copy and the
    open question are in the store when the run starts. Below the copy a
    part of the enclosing project is declared in a folder of its own
    (`essay`), and `essay/deep` holds no file. The records an earlier build
    wrote at `deep`, or at the part's folder and at `deep`, find the copy's
    view as their root, but go to the part, as a session at `deep`
    resolves, not to the provisional project, whose records that session
    is not given: while the question is open, the gate also reads them
    against the root outside the copy's view. The run asks nothing more."""
    store, config = lab
    copy, known, _part = _copy_of_a_part(tmp_path, store, config)
    essay = _declare(store, known, copy / "essay", "essay")
    deep = copy / "essay" / "deep"
    deep.mkdir()
    if asked_by == "session":
        _live(store, config, copy)
    else:
        _rootless(store, copy, "in the copy")
        location.forget()
        migrate(store, location.process_machine(), now=lambda: _NOW)
    (question,) = _pending(store)
    at = {"essay": copy / "essay", "deep": deep}
    ids = [_rootless(store, at[n], f"in {n}") for n in records.split(",")]
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    assert [homes[i][0] for i in ids] == [essay] * len(ids), (homes, essay)
    assert homes[ids[0]][2] == "migration:cwd:declared_folder"
    t = report.totals
    assert (t.projects, t.proposals) == (0, 0)
    assert _pending(store) == [question]
    assert _live(store, config, deep)[0] == essay


@pytest.mark.parametrize("records", ["deep", "essay,deep"])
@pytest.mark.parametrize("answer", ["separate", "join"])
def test_a_part_below_a_copy_whose_question_was_answered_keeps_its_records(
    tmp_path: Path, lab: tuple[Store, Config], answer: str, records: str
) -> None:
    """The copy of a part's file (`_copy_of_a_part`), asked about by a
    session there and answered before this run: a project of its own
    (``separate``: for a folder whose file another project holds, ``project
    init --new``, a fresh file), or joined to the part whose file it copies
    (``join``). Below the copy a part of the enclosing project is declared
    (`essay`), and `essay/deep` holds no file. The records an earlier build
    wrote at `deep`, or at the part's folder and at `deep`, find the copy's
    view as their root, but go to the part, as a session at `deep`
    resolves: the gate reads them against the root outside the copy's view
    whatever its question became (ENG-222). Gated only while the question
    was open, they went by D4 to the copy's own project or to the part it
    joined, whose records a session at `deep` is not given. A record at the
    copy itself still goes by D4 to the project a session there resolves
    to."""
    store, config = lab
    copy, known, part = _copy_of_a_part(tmp_path, store, config)
    essay = _declare(store, known, copy / "essay", "essay")
    deep = copy / "essay" / "deep"
    deep.mkdir()
    _live(store, config, copy)
    (question,) = _pending(store)
    fresh = None
    if answer == "separate":
        fresh = project_file.write(copy, name="copy", rebind=True).id
        location.forget()
    Membership(store, location.process_machine().id).declare(
        Declaration(
            "join" if fresh is None else "init_new",
            "os:tester",
            checkout=question.checkout,
            proposal=question.id,
            target=part if fresh is None else None,
            file_id=fresh,
            name=None if fresh is None else "copy",
            directory=None if fresh is None else str(copy),
        )
    )
    assert _pending(store) == []
    at = {"essay": copy / "essay", "deep": deep}
    ids = [_rootless(store, at[n], f"in {n}") for n in records.split(",")]
    in_copy = _rootless(store, copy, "in the copy")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    assert [homes[i][0] for i in ids] == [essay] * len(ids), (homes, essay)
    assert homes[ids[0]][2] == "migration:cwd:declared_folder"
    settled = _live(store, config, copy)[0]
    assert settled != known.project and (settled == part) == (answer == "join")
    assert homes[in_copy] == (settled, question.checkout, "migration:cwd_in_root")
    t = report.totals
    assert (t.projects, t.proposals) == (0, 0)
    assert _pending(store) == []
    assert _live(store, config, deep)[0] == essay


def test_a_part_declared_inside_another_parts_folder_keeps_its_records(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """`Writing/novel/chapter` declared a part of `Writing` while `novel`
    held no file, and `novel` declared a part afterwards, which a session
    there saw (a view). Records at `chapter` and at `chapter/deep` find
    `novel`'s view as their root, and `chapter`'s file is no part of
    `novel`'s; but the gate reads them against the root outside that view
    too, and they go to `chapter`, as a session there resolves, in both
    birth modes. No question is open about `novel`'s checkout: gated only
    while one was, they went by D4 to `novel` (ENG-222)."""
    store, config = lab
    outer, known = _writing(tmp_path, store, config)
    inner = outer / "novel"
    inner.mkdir()
    chapter = _declare(store, known, inner / "chapter", "chapter")
    novel = _declare(store, known, inner, "novel")
    assert _live(store, config, inner) == (novel, "declared_folder")
    (inner / "chapter" / "deep").mkdir()
    ids = [
        _rootless(store, inner / "chapter", "in the chapter"),
        _rootless(store, inner / "chapter" / "deep", "below the chapter"),
    ]
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    assert [homes[i][0] for i in ids] == [chapter] * 2, (homes, novel)
    assert homes[ids[0]][2] == "migration:cwd:declared_folder"
    assert (report.totals.projects, report.totals.proposals) == (0, 0)
    assert _live(store, config, inner / "chapter" / "deep")[0] == chapter


def test_a_moved_copy_keeps_its_old_paths_records_with_its_question(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A session asks about a copy of a part's file at `Writing/copyA`, and
    the folder is then renamed `copyB`. An earlier build wrote two records
    at `copyB` and one below it, and last one at `copyA`. The copy's
    records keep the enclosing project (D4), and the one at `copyA` keeps
    the asked checkout, whose view there it lies in: what the run passes
    over is the view a resolution added, not the checkout. With birth time
    the ladder finds the asked checkout at `copyB` by its folder's object,
    and gives it a view there; passing that checkout over everywhere would
    send the record at `copyA` to the enclosing project. Without it the
    ladder cannot tell the moved folder is that checkout's (its file names
    the part, not the asked project, and the open question confirms the
    checkout only at a path it was seen at, ENG-219), asks about it again
    and registers a checkout of its own there, which has no other view."""
    store, config = lab
    copy, known, _part = _copy_of_a_part(tmp_path, store, config)
    asked = _live(store, config, copy)
    moved = copy.with_name("copyB")
    copy.rename(moved)
    (moved / "notes").mkdir()
    ids = [
        _rootless(store, moved, "in the moved copy"),
        _rootless(store, moved, "in the moved copy again"),
        _rootless(store, moved / "notes", "below the moved copy"),
    ]
    old = _rootless(store, copy, "at the copy's old path")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    d4 = (known.project, known.checkout, "migration:cwd_in_root")
    assert [homes[i] for i in ids] == [d4] * 3
    assert homes[old] == (asked[0], homes[old][1], "migration:cwd_in_root")
    model = Membership(store, location.process_machine().id)
    k = model.checkout(str(homes[old][1]))
    views = sorted(v.path for v in k.views_on(location.process_machine().id))
    t = report.totals
    if birth_reported(moved):
        assert views == sorted([str(copy), str(moved)])
        assert (t.projects, t.checkouts, t.proposals) == (0, 0, 0)
    else:
        assert views == [str(copy)]
        assert (t.projects, t.checkouts, t.proposals) == (1, 1, 1)


@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_a_moved_repository_copy_keeps_its_old_paths_records_with_its_question(
    tmp_path: Path, lab: tuple[Store, Config], committed: bool
) -> None:
    """The moved copy above as a git repository whose root holds the part's
    file, committed or not (the round-4 review's M6). Without birth time
    too, the ladder finds the moved repository by its git directory and
    token (``object+token``), so the asked checkout gains a view at
    `copyB` in both modes, and the run passes that view over: the copy's
    records keep the enclosing project (D4), and the one at `copyA` keeps
    the asked checkout. Keyed by the checkout alone, the record at `copyA`
    would go to the enclosing project in both modes."""
    store, config = lab
    outer, inner, known, _part = _declared_part(tmp_path, store, config)
    copy = outer / "copyA"
    name = project_file.PROJECT_FILE_NAME
    text = (inner / name).read_text(encoding="utf-8")
    if committed:
        checkout(copy, files={name: text, "other.md": "other\n"})
    else:
        checkout(copy, files={"other.md": "other\n"})
        shutil.copyfile(inner / name, copy / name)
    asked = _live(store, config, copy)
    assert asked[0] != known.project
    moved = copy.with_name("copyB")
    copy.rename(moved)
    (moved / "notes").mkdir()
    ids = [
        _rootless(store, moved, "in the moved copy"),
        _rootless(store, moved, "in the moved copy again"),
        _rootless(store, moved / "notes", "below the moved copy"),
    ]
    old = _rootless(store, copy, "at the copy's old path")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    homes = _homes(report)
    d4 = (known.project, known.checkout, "migration:cwd_in_root")
    assert [homes[i] for i in ids] == [d4] * 3
    assert homes[old] == (asked[0], homes[old][1], "migration:cwd_in_root")
    model = Membership(store, location.process_machine().id)
    k = model.checkout(str(homes[old][1]))
    views = sorted(v.path for v in k.views_on(location.process_machine().id))
    assert views == sorted([str(copy), str(moved)])


def test_a_folder_project_turned_repository_finds_its_sub_project(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A folder project a session resolved, in which `git init` ran later,
    and below it a sub-project's file bound to none: the ladder finds the
    folder's checkout by the working tree root's object, re-keys it to the
    repository (``rekeyed:F+O``, E23 j) and makes the sub-project there, so a
    record with no root at the sub-project's folder goes to it, as a
    session there resolves."""
    store, config = lab
    outer, known = _writing(tmp_path, store, config)
    (outer / "style.md").write_text("style\n", encoding="utf-8")
    _git(outer, "init", "-q", "-b", "main")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "init")
    sub = outer / "pkg"
    sub.mkdir()
    project_file.write(sub, name="pkg")
    in_sub = _rootless(store, sub, "in pkg after git init")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    project, at, rule = _homes(report)[in_sub]
    assert (at, rule) == (known.checkout, "migration:cwd:rekeyed:F+O")
    model = Membership(store, location.process_machine().id)
    assert model.chain_of(project) == [project, known.project]
    assert _live(store, config, sub)[0] == project


def test_a_sub_project_a_session_bound_keeps_its_records(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A repository whose sub-project file a session at its folder already
    bound, and a record an earlier build wrote there, with no root: the
    file is bound to a part of the repository's project, so the ladder runs
    and finds the sub-project, as that session did."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    sub = repo / "pkg"
    sub.mkdir()
    written = project_file.write(sub, name="pkg")
    bound = _live(store, config, sub)[0]
    model = Membership(store, location.process_machine().id)
    assert bound is not None and bound == model.file_binding(written.id)
    _rooted(store, repo)
    in_sub = _rootless(store, sub, "in the sub-project")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    (root,) = report.roots
    assert model.project(bound).parent == root.project
    assert _homes(report)[in_sub] == (
        bound,
        root.checkout,
        "migration:cwd:" + known_rule(repo),
    )


def test_a_nested_file_bound_to_the_roots_own_project_leaves_the_gate_open(
    tmp_path: Path, lab: tuple[Store, Config]
) -> None:
    """A repository with a recorded root and its own project file, a copy
    of that file in `docs` (bound to the repository's project once the run
    resolves the root), and below it `docs/pkg` with a file bound to none:
    the file bound to the root's own project is no outside binding, so the
    ladder runs at `docs/pkg` and makes the sub-project, as a session there
    does."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    project_file.write(repo, name="app")
    sub = repo / "docs" / "pkg"
    sub.mkdir(parents=True)
    shutil.copyfile(
        repo / project_file.PROJECT_FILE_NAME,
        repo / "docs" / project_file.PROJECT_FILE_NAME,
    )
    written = project_file.write(sub, name="pkg")
    _rooted(store, repo)
    in_sub = _rootless(store, sub, "in docs/pkg")
    location.forget()

    report = migrate(store, location.process_machine(), now=lambda: _NOW)

    (root,) = report.roots
    project, at, rule = _homes(report)[in_sub]
    assert (at, rule[: len("migration:cwd:")]) == (root.checkout, "migration:cwd:")
    model = Membership(store, location.process_machine().id)
    assert project == model.file_binding(written.id)
    assert model.chain_of(project) == [project, root.project]
    assert _live(store, config, sub)[0] == project


def _run(store: Store) -> mm._Run:
    """A migration run over `store`, its methods called one at a time."""
    return mm._Run(
        store,
        location.process_machine(),
        now=lambda: _NOW,
        write_token=False,
        strict=False,
    )


def test_a_checkout_of_another_machine_is_not_the_cwds_repository(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's repository check (`_Run._same_repository`) holds only for
    a checkout of this machine, as the ladder finds only those: the same
    repository's checkout, read as another machine's, is not found."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    (repo / "pkg").mkdir()
    res = resolve_caller(store, config, str(repo)).resolution
    assert res.checkout is not None
    run = _run(store)
    cap = run._probe(str(repo / "pkg"), at_root=False).capture
    assert cap is not None
    loc = run._location(cap)
    assert run._same_repository(loc, res.checkout)
    real = run.model.checkout
    monkeypatch.setattr(
        run.model,
        "checkout",
        lambda kid: dataclasses.replace(real(kid), machine="another-machine"),
    )
    assert not run._same_repository(loc, res.checkout)


def test_a_checkout_with_no_object_store_key_is_found_from_its_worktree(
    tmp_path: Path, lab: tuple[Store, Config], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A primary checkout whose row carries no object store key (a row an
    earlier build wrote), and a linked worktree of it: the ladder finds the
    checkout by its git directory, which is the worktree's object store
    (`Membership._by_common` reads both columns), and so does the gate's
    repository check."""
    store, config = lab
    repo = checkout(tmp_path / "work" / "app", files={"src/main.txt": "main\n"})
    tree = tmp_path / "work" / "feature"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(tree))
    res = resolve_caller(store, config, str(repo)).resolution
    assert res.checkout is not None
    run = _run(store)
    cap = run._probe(str(tree), at_root=False).capture
    assert cap is not None
    loc = run._location(cap)
    real = run.model.checkout
    monkeypatch.setattr(
        run.model,
        "checkout",
        lambda kid: dataclasses.replace(real(kid), common_object_key=None),
    )
    assert run.model.checkout(res.checkout).common_object_key is None
    assert run._same_repository(loc, res.checkout)

"""A folder the ladder asks about on its project file alone is the same
checkout at every later session, its question asked again, with birth
time and without it (ENG-219).

A folder holding a copy of a part's project file and other content
(`test_membership_migrate_parts._copy_of_a_part`) is one witness, F: the
first session there is asked (``ask:one_witness``), which registers a
provisional project, a checkout with a view at the folder and one open
``first_sight`` question. A later session finds that checkout by the
folder's object (rung 2, O). With birth time the object decides; without
it (NFS, SMB, some containers) a folder checkout is continued by its
project file bound to its project (decision B20), which an asked
checkout never has: the file names the project asked about. So each
session there minted a provisional project and opened a question of its
own. Now, where rung 2 continues no checkout, the ladder takes the one
the folder was asked about, its question open on the evidence the folder
still shows, at a path the checkout was seen at
(`membership_resolve._asked_folder`), and later sessions there, below it
too, are asked that question; so are sessions at a folder a migration
run asked about (ENG-212).

What the question is about must not be lent to another folder: a copy
of the same file elsewhere, the asked folder moved to another path, and
the folder with another file in it are each asked about anew without
birth time. With birth time the moved folder and the refiled one are the
asked checkout's own object, as before. Nor does an open question
outrank an answer: a checkout the user's answer joined to the part,
which rung 2 continues by its bound file, is taken over a provisional one
asked about the same folder (duplicates a build before this one left
without birth time, a folder answered at the path it was moved to and
moved back).

Every test runs as the filesystem reports birth time and as it reports
none (the `no_birth` fixture), so both are run in every leg; under
``BETTERMEMORY_TEST_NO_BIRTH=1`` both read none.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bettermemory import location, membership_resolve, project_file
from bettermemory.config import Config
from bettermemory.location import resolve_caller
from bettermemory.membership import (
    ASK,
    SILENT,
    Declaration,
    Location,
    Membership,
    Resolution,
)
from bettermemory.membership_migrate import migrate
from bettermemory.store import Store

from .test_membership_hung_view import _NOW, lab
from .test_membership_migrate_parts import (
    _copy_of_a_part,
    _declare,
    _pending,
    _rootless,
)
from .test_membership_resolution import birth_reported, no_birth, posing

__all__ = ["lab", "no_birth"]


@pytest.fixture(params=["birth", "no birth"])
def birth(request: pytest.FixtureRequest) -> str:
    """The test as the filesystem reports birth time, and as it reports
    none (`no_birth`)."""
    if request.param == "no birth":
        request.getfixturevalue("no_birth")
        assert not location.REPORT_BIRTH
    return str(request.param)


def _session(store: Store, config: Config, cwd: Path) -> Resolution:
    """A session's resolution at `cwd`, captured afresh."""
    location.forget()
    return resolve_caller(store, config, str(cwd)).resolution


def _counts(store: Store) -> tuple[int, int, int]:
    """The store's projects, checkouts and open questions."""
    model = Membership(store, location.process_machine().id)
    return len(model.projects()), len(model.checkouts()), len(model.pending())


def _asked(res: Resolution) -> tuple[str, str | None, str | None, str | None]:
    """What a resolution at an asked folder shows: its outcome, project,
    checkout and question."""
    return res.outcome, res.project, res.checkout, res.proposal


def test_later_sessions_at_a_folder_asked_on_its_file_are_asked_its_question(
    tmp_path: Path, birth: str, lab: tuple[Store, Config]
) -> None:
    """The first session at the copy of a part's file is asked; the second
    and the third, and one in a folder below it, resolve to its
    provisional checkout by the folder's object and are asked its open
    question. No project, checkout or question is added after the first,
    in both birth modes."""
    store, config = lab
    copy, _known, part = _copy_of_a_part(tmp_path, store, config)
    (copy / "notes").mkdir()
    first = _session(store, config, copy)
    assert (first.outcome, first.rule) == (ASK, "ask:one_witness")
    (question,) = _pending(store)
    assert (question.id, question.candidates) == (first.proposal, (part,))
    before = _counts(store)

    later = [_session(store, config, at) for at in (copy, copy, copy / "notes")]

    assert [_asked(r) for r in later] == [_asked(first)] * 3
    assert {r.rule for r in later} == {"folder_object"}
    assert _counts(store) == before
    assert _pending(store) == [question]


def test_sessions_at_a_folder_a_migration_asked_about_are_asked_its_question(
    tmp_path: Path, birth: str, lab: tuple[Store, Config]
) -> None:
    """A migration run whose record lies at the copy of a part's file asks
    about the copy as a session's first sight there would (ENG-212). The
    sessions after it at the copy resolve to the checkout the run
    registered, in its provisional project, and are asked the question the
    run left, opening none of their own, in both birth modes."""
    store, config = lab
    copy, _known, part = _copy_of_a_part(tmp_path, store, config)
    _rootless(store, copy, "in the copy")
    location.forget()
    migrate(store, location.process_machine(), now=lambda: _NOW)
    (question,) = _pending(store)
    assert (question.reason, question.candidates) == ("one_witness", (part,))
    model = Membership(store, location.process_machine().id)
    asked = model.checkout(str(question.checkout))
    before = _counts(store)

    seen = [_session(store, config, copy) for _ in range(2)]

    assert [_asked(r) for r in seen] == [
        (ASK, model.canonical(asked.project), asked.id, question.id)
    ] * 2
    assert _counts(store) == before
    assert _pending(store) == [question]


@pytest.mark.parametrize("change", ["copied", "moved", "refiled"])
def test_a_folder_not_shown_to_be_the_asked_one_is_asked_anew(
    tmp_path: Path, birth: str, lab: tuple[Store, Config], change: str
) -> None:
    """The asked copy of a part's file, then a session at another folder
    that shows its file: a second copy of the file elsewhere (another
    object), the asked folder renamed (``moved``: its object, at a path the
    checkout was never seen at), or the asked folder with the file of
    another part copied over its own (``refiled``: its object, at its path,
    but not the evidence its question was asked on). Without birth time
    each is asked about anew: a provisional project, a checkout and a
    question of its own. With birth time the copy elsewhere is asked too,
    and the moved and the refiled folder are the asked checkout's own
    object, asked its question."""
    store, config = lab
    copy, known, _part = _copy_of_a_part(tmp_path, store, config)
    first = _session(store, config, copy)
    assert first.outcome == ASK
    if change == "copied":
        where = copy.with_name("copy2")
        where.mkdir()
        (where / "other.md").write_text("other\n", encoding="utf-8")
        shutil.copyfile(
            copy / project_file.PROJECT_FILE_NAME,
            where / project_file.PROJECT_FILE_NAME,
        )
    elif change == "moved":
        where = copy.rename(copy.with_name("moved"))
    else:
        poem = copy.with_name("poem")
        _declare(store, known, poem, "poem")
        shutil.copyfile(
            poem / project_file.PROJECT_FILE_NAME,
            copy / project_file.PROJECT_FILE_NAME,
        )
        where = copy
    projects, checkouts, questions = _counts(store)

    res = _session(store, config, where)

    if change != "copied" and birth_reported(where):
        assert _asked(res) == _asked(first) and res.rule == "folder_object"
        assert _counts(store) == (projects, checkouts, questions)
    else:
        assert (res.outcome, res.rule) == (ASK, "ask:one_witness")
        assert res.project != first.project and res.checkout != first.checkout
        assert res.proposal not in (None, first.proposal)
        assert _counts(store) == (projects + 1, checkouts + 1, questions + 1)


def test_a_folder_made_again_with_the_asked_number_is_asked_anew_with_birth_time(
    tmp_path: Path, birth: str, lab: tuple[Store, Config]
) -> None:
    """The asked copy removed and a folder made at its path with the same
    file and other content, the capture reading the old folder's device
    and inode number for it (a filesystem handing a freed number back).
    With birth time its own birth time tells it apart, and it is asked
    about anew; without it nothing does, and at the asked path, with the
    asked file, it is the asked checkout, asked its question."""
    store, config = lab
    copy, _known, _part = _copy_of_a_part(tmp_path, store, config)
    name = project_file.PROJECT_FILE_NAME
    text = (copy / name).read_bytes()
    first = _session(store, config, copy)
    old = Membership(store, location.process_machine().id).checkout(str(first.checkout))
    assert old.object_key is not None
    shutil.rmtree(copy)
    copy.mkdir()
    (copy / "unrelated.md").write_text("another folder\n", encoding="utf-8")
    (copy / name).write_bytes(text)
    real = location.object_key(str(copy))
    assert real is not None
    born = None if real[2] is None else str(int(real[2]) + 1)
    counts = _counts(store)

    with posing(copy, (old.object_key.device, old.object_key.inode, born)):
        res = _session(store, config, copy)

    if born is not None:
        assert (res.outcome, res.rule) == (ASK, "ask:one_witness")
        assert res.checkout != first.checkout
        assert _counts(store) == (counts[0] + 1, counts[1] + 1, counts[2] + 1)
    else:
        assert _asked(res) == _asked(first) and res.rule == "folder_object"
        assert _counts(store) == counts


def _no_asked_folder(self: Membership, loc: Location) -> None:
    """A build before this one: no checkout is taken for the question
    open about a folder."""


@pytest.mark.parametrize("left", ["duplicates", "moved back"])
def test_a_checkout_an_answer_settled_outranks_an_open_question_there(
    tmp_path: Path,
    birth: str,
    lab: tuple[Store, Config],
    monkeypatch: pytest.MonkeyPatch,
    left: str,
) -> None:
    """Checkouts with the copy's object, one of them joined to the part by
    the user's answer and another still asked about: ``duplicates``, three
    sessions a build before this one left at the copy, each without birth
    time with a provisional checkout and a question of its own, the last
    one shown answered; ``moved back``, the copy asked about, renamed,
    asked about at its new path and answered there, then renamed back. A
    session at the copy resolves silently to the part, at the answered
    checkout, which rung 2 continues by its bound file, and asks none of
    the questions still open, in both birth modes (with birth time there is
    one checkout, the answered one)."""
    store, config = lab
    copy, _known, part = _copy_of_a_part(tmp_path, store, config)
    moved = copy.with_name("moved")
    if left == "duplicates":
        with monkeypatch.context() as before:
            before.setattr(membership_resolve, "_asked_folder", _no_asked_folder)
            answered = [_session(store, config, copy) for _ in range(3)][-1]
    else:
        _session(store, config, copy)
        copy.rename(moved)
        answered = _session(store, config, moved)
    assert answered.outcome == ASK
    Membership(store, location.process_machine().id).declare(
        Declaration(
            "join",
            "os:tester",
            checkout=answered.checkout,
            proposal=answered.proposal,
            target=part,
            said="the same project",
        )
    )
    if left == "moved back":
        moved.rename(copy)
    counts = _counts(store)

    res = _session(store, config, copy)

    assert _asked(res) == (SILENT, part, answered.checkout, None)
    assert res.rule == "folder_object"
    assert _counts(store) == counts

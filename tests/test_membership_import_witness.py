"""An import takes a witness only as a full commit id (ENG-157).

A project identity in a membership export carries its witness commits,
and an import mints the imported project with them as witness rows. A
witness row is read by `HistoryReader.holds`, which expands an
abbreviation to whichever object a repository holds with that prefix: an
export document naming a short prefix as a witness made every repository
holding an object with that prefix hold the imported project's history (H),
so it joined, or tied with, a project it shares nothing with. The
migration has read a recorded commit as a witness only when it is a full id
(`_FULL_SHA`, decision B19); `PortableIdentity.from_json` now reads an
import's witnesses through the same validation and refuses the document.

Two residuals of that fix (ENG-193): the one writer of a witness row
(`Membership._add_witness`) took any id its caller gave, and a store that
imported such a document before 9.0.0rc14 keeps the abbreviated row. The
writer now refuses an id that is not a full one, and a row already in a
store that is not a full id reads as absent wherever witness rows are
read; the store's format does not change.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from bettermemory.config import Config, StorageConfig
from bettermemory.location import resolve_caller
from bettermemory.membership import NEW, Membership, PortableIdentity
from bettermemory.store import Store

from .test_membership_cli import Scene, bm, scene
from .test_membership_security import _git, neutral_git

__all__ = ["scene"]

_SHA1 = "3f786850e387550fdab836ed7e6dc881de23001b"
_SHA256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.mark.parametrize(
    "witness",
    [
        _SHA1[:4],
        _SHA1[:7],
        _SHA1[:12],
        _SHA1[:39],
        _SHA1 + "0",
        _SHA256[:63],
        "g" * 40,
        _SHA1[:20] + " " + _SHA1[21:],
    ],
)
def test_an_identity_whose_witness_is_not_a_full_id_is_refused(witness: str) -> None:
    """An abbreviation (of git's shortest and up), an id one digit too
    long or short, or a string that is not hex is refused, the error
    naming it."""
    raw: dict[str, Any] = {"name": "theirs", "witnesses": [_SHA256, witness]}
    with pytest.raises(ValueError, match="not a full commit id"):
        PortableIdentity.from_json(raw)


def test_an_identity_takes_full_ids_of_either_hash_in_lowercase() -> None:
    """A full SHA-1 or SHA-256 id is taken, in lowercase as a witness row
    carries it."""
    raw: dict[str, Any] = {"name": "ours", "witnesses": [_SHA1.upper(), _SHA256]}
    assert PortableIdentity.from_json(raw).witnesses == (_SHA1, _SHA256)


def test_import_refuses_a_document_with_an_abbreviated_witness(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review's scenario (G5): `bettermemory membership import` of a
    document whose identity names a commit by a seven-digit prefix exits 2
    naming it, and mints nothing: no project, no witness row."""
    repo = scene.repo("app")
    scene.see(repo)
    head = repo.head
    assert head is not None
    projects = len(scene.store.membership_rows("projects"))
    witnesses = scene.store.membership_rows("project_witnesses")
    doc = scene.root / "theirs.json"
    doc.write_text(
        json.dumps(
            {
                "store_id": "S-theirs",
                "projects": [{"name": "theirs", "witnesses": [head[:7]]}],
                "homes": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        bm(monkeypatch, "membership", "import", str(doc))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("bettermemory: ") and "not a full commit id" in err
    assert repr(head[:7]) in err
    assert len(scene.store.membership_rows("projects")) == projects
    assert scene.store.membership_rows("project_witnesses") == witnesses


@pytest.mark.parametrize(
    "witness",
    [_SHA1[:7], _SHA1[:39], _SHA1 + "0", "g" * 40, _SHA1.upper(), ""],
)
def test_the_witness_writer_refuses_an_id_that_is_not_a_full_one(
    scene: Scene, witness: str
) -> None:
    """`Membership.add_witness`, the one writer of a witness row (HEAD at a
    sighting or a write, the migration, an import), refuses an
    abbreviation, an id one digit too long or short, one that is not hex,
    not in lowercase or empty, the error naming it, and writes nothing."""
    repo = scene.repo("app")
    res = scene.see(repo)
    assert res.project is not None
    witnesses = scene.store.membership_rows("project_witnesses")

    with pytest.raises(ValueError, match="not a full commit id"):
        scene.model.add_witness(res.project, witness, source="test")

    assert scene.store.membership_rows("project_witnesses") == witnesses


def _row(project: str, commit: str, arrival: int) -> dict[str, Any]:
    return {
        "project": project,
        "commit_id": commit,
        "arrival": arrival,
        "source": "import:theirs.json",
        "fingerprint": None,
    }


def _mixed(commit: str) -> str:
    """`commit` with every other hex letter in upper case."""
    return "".join(c.upper() if i % 2 else c for i, c in enumerate(commit))


@pytest.mark.parametrize("spelled", [str.upper, _mixed], ids=["upper", "mixed"])
def test_a_full_witness_row_in_upper_case_reads_as_that_commit(
    scene: Scene, spelled: Any
) -> None:
    """An import before 9.0.0rc14 stored a document's witness as given, a
    full id in upper or mixed case included. It reads as that commit in
    lowercase: the project's witness commits name it, an export names it
    as the import's check takes it, and the writer, given the same commit,
    writes no second row (`_has_witness` compares in lowercase)."""
    repo = scene.repo("app")
    scene.see(repo)
    head = repo.head
    assert head is not None
    theirs = scene.model.import_identity(
        PortableIdentity(name="theirs"), source="theirs.json"
    ).project
    scene.store.put_membership("project_witness", _row(theirs, spelled(head), 900))

    assert scene.model.portable_identity(theirs).witnesses == (head,)
    scene.model.add_witness(theirs, head, source="test")
    rows = scene.store.membership_rows("project_witnesses", where={"project": theirs})
    assert [r["commit_id"] for r in rows] == [spelled(head)]
    assert scene.model.witness_commits(theirs) == [head]


def test_an_import_finds_a_project_by_a_witness_row_written_in_upper_case(
    scene: Scene,
) -> None:
    """An import looks a document's witnesses (in lowercase,
    `PortableIdentity.from_json`) up by the index on the witness rows, in
    lowercase and in upper case: a project whose row an import before
    9.0.0rc14 wrote in upper case holds that commit (H) for the next
    import, as it does for a resolution. A row in mixed case, which no
    writer spelled, is not looked up by the index; a resolution, which
    reads every row, and the writer's own check (`_has_witness`) read it
    as that commit."""
    spelled = str.upper
    repo = scene.repo("app")
    scene.see(repo)
    head = repo.head
    assert head is not None
    theirs = scene.model.import_identity(
        PortableIdentity(name="theirs"), source="theirs.json"
    ).project
    scene.store.put_membership("project_witness", _row(theirs, spelled(head), 900))

    again = scene.model.import_identity(
        PortableIdentity(name="again", witnesses=(head,)), source="again.json"
    )

    assert again.codes.get(theirs) == ("H",), again


def test_project_show_counts_the_witnesses_the_model_reads(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``bettermemory project show`` counts a project's witness commits as
    the model reads them: an abbreviated row an import before 9.0.0rc14
    wrote is not counted."""
    repo = scene.repo("app")
    res = scene.see(repo)
    assert res.project is not None and repo.head is not None
    full = scene.store.membership_rows(
        "project_witnesses", where={"project": res.project}
    )
    counted = len(full)
    scene.store.put_membership("project_witness", _row(res.project, repo.head[:7], 900))
    capsys.readouterr()

    bm(monkeypatch, "project", "show", res.project)

    assert f"  witnesses: {counted} commits\n" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_an_abbreviated_witness_row_already_in_a_store_reads_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that imported a document before 9.0.0rc14 keeps a witness as
    the document gave it: here an imported project whose one witness row,
    written directly, is a seven-digit prefix of the commit a repository
    of the user's holds. `HistoryReader.holds` expands the prefix, so that
    repository's first sighting asked whether it is the imported project.
    It is a new project with no question, as in a store without the row;
    an export of the imported project names no witness, so another store's
    import takes it; and the row is kept as written."""
    neutral_git(tmp_path, monkeypatch)
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    head = _git(repo, "rev-parse", "HEAD")
    store = Store(tmp_path / "store")
    try:
        config = Config(storage=StorageConfig(directory=str(tmp_path / "store")))
        model = Membership(store, "machine")
        theirs = model.import_identity(
            PortableIdentity(name="theirs"), source="theirs.json"
        ).project
        row = {
            "project": theirs,
            "commit_id": head[:7],
            "arrival": store.next_membership_number("project_witnesses", "arrival"),
            "source": "import:theirs.json",
            "fingerprint": None,
        }
        store.put_membership("project_witness", row)

        res = resolve_caller(store, config, str(repo)).resolution

        assert (res.outcome, res.proposal) == (NEW, None), res
        assert theirs not in res.candidates and res.project != theirs
        exported = model.portable_identity(theirs)
        assert exported.witnesses == ()
        assert PortableIdentity.from_json(exported.to_json()) == exported
        assert store.membership_rows(
            "project_witnesses", where={"project": theirs}
        ) == [row]
    finally:
        store.close()

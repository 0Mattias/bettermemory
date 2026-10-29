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
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bettermemory.membership import PortableIdentity

from .test_membership_cli import Scene, bm, scene

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

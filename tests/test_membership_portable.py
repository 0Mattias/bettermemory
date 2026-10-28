"""A project's portable identity across stores (RS-F6; design 8.7, E27,
I37a, T16): `bettermemory membership export | import`, the memory export
carrying the same, and `Membership.import_identity`'s mapping by the file
id (F), by history and remote together (H+R), by one of them (an
``import`` question, line L10, in the checkout holding the evidence), or
by none (a project with no live checkout that a later checkout joins).

Store A is a teammate's, opened here as a second store with a machine of
its own; store B is the CLI's (`tests.test_membership_cli.Scene`). The
records an import homes are present in B without a home, as records that
arrived by another path are.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import project_file
from bettermemory.membership import (
    ASK,
    GLOBAL,
    IMPORT,
    IMPORT_CONTRADICTION,
    NEW,
    SESSION_BRIEF,
    SILENT,
    Declaration,
    Membership,
    PortableIdentity,
    Resolution,
    admitted,
    portable_export,
)
from bettermemory.store import RecordHome, Store

from .test_membership_cli import History, Repo, Scene, bm, scene, typed

__all__ = ["scene"]

URL1 = "example.invalid/acme/one"
URL2 = "example.invalid/acme/two"
URL4 = "example.invalid/acme/four"


class Side:
    """A second store with a machine of its own."""

    def __init__(
        self, path: Path, machine: str, *, keys_dir: Path | None = None
    ) -> None:
        self.path = path
        self.store = Store(path, keys_dir=keys_dir)
        self.machine = machine

    @property
    def model(self) -> Membership:
        return Membership(self.store, self.machine)

    def see(self, repo: Repo) -> Resolution:
        return self.model.resolve(repo.location(self.machine))


def make_repo(path: Path, *, commits: int = 2, remote: str | None = None) -> Repo:
    (path / ".git" / "info").mkdir(parents=True)
    repo = Repo(path=path, history=History(), remotes={remote} if remote else set())
    for _ in range(commits):
        repo.commit()
    return repo


def clone_of(repo: Repo, path: Path, *, remote: str | None = None) -> Repo:
    twin = make_repo(path, commits=0)
    twin.history = repo.history.copy()
    twin.head = repo.head
    twin.remotes = {remote} if remote else set()
    return twin


def put_record(store: Store, rid: str | None, home: RecordHome | None) -> str:
    from bettermemory.models import Confidence, Memory, Source, generate_ulid, utcnow

    rid = rid or generate_ulid()
    now = utcnow()
    store.put_memory(
        Memory(
            id=rid,
            created=now,
            updated=now,
            scopes=["tools"],
            confidence=Confidence.MEDIUM,
            source=Source.EXPLICIT,
            body=f"memory {rid}\n",
        ),
        home=home,
    )
    return rid


@pytest.fixture
def teammate(tmp_path: Path) -> Iterator[Side]:
    side = Side(tmp_path.resolve() / "teammate-store", "teammate")
    yield side
    side.store.close()


def _as_cli_store(monkeypatch: pytest.MonkeyPatch, side: Side) -> None:
    monkeypatch.setenv("BETTERMEMORY_DIR", str(side.path))


def _teammates_records(root: Path, a: Side) -> tuple[dict[str, str], dict[str, Repo]]:
    """Store A's records: (a) in a repository whose project file is
    committed, (b) in one with a remote, (c) in a remote-less one, (d) in
    one store B never saw, (f) outside any repository."""
    one = make_repo(root / "src" / "one", remote=URL1)
    a.see(one)
    project_file.write(one.path, name="one")
    one.commit()
    r_one = put_record(a.store, None, a.see(one).home)
    two = make_repo(root / "src" / "two", remote=URL2)
    r_two = put_record(a.store, None, a.see(two).home)
    three = make_repo(root / "src" / "three")
    r_three = put_record(a.store, None, a.see(three).home)
    four = make_repo(root / "src" / "four", remote=URL4)
    r_four = put_record(a.store, None, a.see(four).home)
    r_global = put_record(a.store, None, RecordHome(GLOBAL, None, "outside_repository"))
    records = {
        "one": r_one,
        "two": r_two,
        "three": r_three,
        "four": r_four,
        "global": r_global,
    }
    return records, {"one": one, "two": two, "three": three, "four": four}


def _local_views(scene: Scene, sources: dict[str, Repo]) -> dict[str, Repo]:
    """Store B's own checkouts: a clone of one with the committed file and
    no remote, a clone of two with its remote, a copy of three."""
    b1 = clone_of(sources["one"], scene.root / "b" / "one")
    (b1.path / project_file.PROJECT_FILE_NAME).write_bytes(
        (sources["one"].path / project_file.PROJECT_FILE_NAME).read_bytes()
    )
    b2 = clone_of(sources["two"], scene.root / "b" / "two", remote=URL2)
    b3 = clone_of(sources["three"], scene.root / "b" / "three")
    for repo in (b1, b2, b3):
        assert scene.see(repo).outcome == NEW
    return {"one": b1, "two": b2, "three": b3}


def test_t16_e27_import_maps_by_file_history_and_remote_one_or_none(
    scene: Scene,
    teammate: Side,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records, sources = _teammates_records(scene.root / "a", teammate)
    mine = _local_views(scene, sources)
    for rid in records.values():
        put_record(scene.store, rid, None)
    exported = scene.root / "teammate.json"
    _as_cli_store(monkeypatch, teammate)
    bm(monkeypatch, "membership", "export", "-o", str(exported))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(scene.root / "store"))
    doc = json.loads(exported.read_text(encoding="utf-8"))
    blob = exported.read_text(encoding="utf-8")
    # (e) no local id and no path travels; the source store by its own id.
    assert not re.search(r"\b[PKQ]\d{3}\b", blob)
    assert str(scene.root) not in blob
    assert doc["store_id"] == teammate.store.membership_store_id
    assert doc["format"] == "bettermemory membership export"
    assert set(doc["homes"]) == set(records.values())
    capsys.readouterr()

    bm(monkeypatch, "membership", "import", str(exported))
    out = capsys.readouterr().out
    assert "homed 5 records; 0 already homed; 0 not in this store" in out
    model = scene.model
    home = {name: model.home(rid) for name, rid in records.items()}
    assert all(h is not None for h in home.values())

    # (a) by the file id, which each store bound from its own evidence.
    b1 = scene.see(mine["one"])
    one = home["one"]
    assert one is not None and one.project == b1.project
    assert one.rule.startswith("import:F")
    # (b) by history and remote together: silent, delivered there.
    b2 = scene.see(mine["two"])
    two = home["two"]
    assert two is not None and (two.project, two.rule) == (b2.project, "import:H+R")
    assert admitted(SESSION_BRIEF, two, b2)
    # (c) by history alone: kept apart, asked once in the checkout holding it.
    three = home["three"]
    assert three is not None and three.rule == "import:ask:import_one_witness"
    b3 = scene.see(mine["three"])
    assert b3.outcome == ASK and not admitted(SESSION_BRIEF, three, b3)
    asked = [pr for pr in model.pending() if pr.kind == IMPORT]
    assert [pr.checkout for pr in asked] == [b3.checkout]
    line = asked[0].line
    assert line == (
        f'bettermemory: imported project "three" from teammate.json may be "three" '
        f"({b3.project}: H). Same project, when the user says so: "
        f'memory_admin(action="answer_question", id="{asked[0].id}", answer="join", '
        f'target="{b3.project}", said="<the user\'s words>") (in that checkout); '
        f'keep apart: memory_admin(action="answer_question", id="{asked[0].id}", '
        'answer="separate")'
    )
    assert line in b3.lines and line in out
    typed(monkeypatch, "three")
    bm(monkeypatch, "project", "join", str(b3.project))
    joined = scene.see(mine["three"])
    assert joined.outcome == SILENT and joined.project == b3.project
    three = scene.model.home(records["three"])
    assert three is not None and admitted(SESSION_BRIEF, three, joined)
    # (d) by neither: a project with no live checkout here, which a later
    # clone joins by history and remote, with the record.
    four = home["four"]
    assert four is not None and four.rule == "import:new:no_live_checkout"
    later = clone_of(sources["four"], scene.root / "b" / "four", remote=URL4)
    b4 = scene.see(later)
    assert (b4.outcome, b4.project, b4.rule) == (SILENT, four.project, "join:H+R")
    assert admitted(SESSION_BRIEF, four, b4)
    # (f) a global record stays global.
    glob = home["global"]
    assert glob is not None and (glob.project, glob.rule) == (GLOBAL, "import:global")
    # Each homing is a row naming the rule and the source.
    homed = [
        json.loads(r.payload) for r in scene.store.membership_log(["import_homed"])
    ]
    assert {h["record"] for h in homed} == set(records.values())
    assert {h["source"] for h in homed} == {"teammate.json"}

    # A second import homes nothing again and mints nothing.
    projects = scene.store.membership_rows("projects")
    proposals = scene.store.membership_rows("proposals")
    bm(monkeypatch, "membership", "import", str(exported))
    assert "homed 0 records; 5 already homed" in capsys.readouterr().out
    assert scene.store.membership_rows("projects") == projects
    assert scene.store.membership_rows("proposals") == proposals


def test_the_memory_export_carries_what_an_import_needs(
    scene: Scene,
    teammate: Side,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    records, sources = _teammates_records(scene.root / "a", teammate)
    mine = _local_views(scene, sources)
    put_record(scene.store, records["two"], None)
    exported = scene.root / "memories.json"
    _as_cli_store(monkeypatch, teammate)
    bm(monkeypatch, "export", "-o", str(exported))
    monkeypatch.setenv("BETTERMEMORY_DIR", str(scene.root / "store"))
    doc = json.loads(exported.read_text(encoding="utf-8"))
    part = doc["membership"]
    assert part["store_id"] == teammate.store.membership_store_id
    assert set(part["homes"]) == {m["id"] for m in doc["active_memories"]}
    names = [p["name"] for p in part["projects"]]
    assert sorted(names) == ["four", "one", "three", "two"]
    capsys.readouterr()
    bm(monkeypatch, "membership", "import", str(exported))
    out = capsys.readouterr().out
    assert "homed 1 records; 0 already homed; 4 not in this store" in out
    home = scene.model.home(records["two"])
    b2 = scene.see(mine["two"])
    assert home is not None and (home.project, home.rule) == (b2.project, "import:H+R")


def test_i37a_a_store_cloned_whole_forks_at_import(
    scene: Scene,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = scene.repo("app")
    first = scene.see(app)
    scene.write(first)
    checkouts = scene.store.membership_rows("checkouts")
    store_dir = scene.root / "store"
    keys = scene.store.keys_dir
    scene.store.close()
    image = tmp_path.resolve() / "booted-twice"
    shutil.copytree(store_dir, image)
    # The image carries its key ring too, bound on its own machine to its
    # own copy of the store: one key ring is never two stores' to write.
    image_keys = tmp_path.resolve() / "booted-twice-keys"
    shutil.copytree(keys, image_keys, ignore=shutil.ignore_patterns("*.path*"))
    scene.store = Store(store_dir)
    twin = Side(image, scene.machine, keys_dir=image_keys)
    try:
        assert twin.store.membership_store_id == scene.store.membership_store_id
        elsewhere = make_repo(scene.root / "twin" / "work")
        written = put_record(twin.store, None, twin.see(elsewhere).home)
        exported = scene.root / "twin.json"
        exported.write_text(json.dumps(portable_export(twin.store)), encoding="utf-8")
    finally:
        twin.store.close()
    old = scene.store.membership_store_id
    bm(monkeypatch, "membership", "import", str(exported))
    out = capsys.readouterr().out
    assert f"store forked: record {written} carries this store's id {old}" in out
    assert "not in this store" in out
    new = scene.store.membership_store_id
    assert new != old
    forked = [
        json.loads(r.payload) for r in scene.store.membership_log(["store_forked"])
    ]
    assert forked == [
        {"old_store": old, "new_store": new, "record": written, "source": str(exported)}
    ]
    assert scene.store.membership_rows("checkouts") == checkouts
    assert scene.model.home(written) is None
    bm(monkeypatch, "membership", "import", str(exported))
    assert "store forked" not in capsys.readouterr().out
    assert len(list(scene.store.membership_log(["store_forked"]))) == 1


def test_an_imported_file_over_another_projects_history_asks_in_both_checkouts(
    scene: Scene,
) -> None:
    x_repo = scene.repo("x")
    project_file.write(x_repo.path, name="x")
    x = scene.see(x_repo)
    y_repo = scene.repo("y")
    y = scene.see(y_repo)
    fid = project_file.parse(x_repo.path / project_file.PROJECT_FILE_NAME)
    assert isinstance(fid, project_file.ProjectFile)
    identity = PortableIdentity(
        name="theirs", file_ids=(fid.id,), witnesses=(str(y_repo.head),)
    )
    model = scene.model
    mapped = model.import_identity(identity, source="theirs.json", source_store="S")
    assert mapped.rule == f"import:ask:{IMPORT_CONTRADICTION}"
    assert dict(mapped.codes) == {x.project: ("F",), y.project: ("H",)}
    asked = [model.proposal(p) for p in mapped.proposals]
    assert {pr.checkout for pr in asked} == {x.checkout, y.checkout}
    assert model.project(mapped.project).provisional == IMPORT
    rid = put_record(scene.store, None, None)
    assert model.import_home(rid, "memory", mapped, source="theirs.json")
    assert not model.import_home(rid, "memory", mapped, source="theirs.json")
    # Answered in one checkout, the other question closes with it.
    done = scene.model.declare(
        Declaration(
            "new",
            "os:tester",
            checkout=asked[0].checkout,
            proposal=asked[0].id,
        )
    )
    assert done.project is not None
    assert scene.model.pending() == []
    assert scene.model.project(mapped.project).provisional is None
    home = scene.model.home(rid)
    assert home is not None and home.project == mapped.project
    # The same identity from the same store maps to that project again.
    again = scene.model.import_identity(
        identity, source="theirs.json", source_store="S"
    )
    assert (again.project, again.rule) == (mapped.project, "import:earlier")


def test_the_portable_identity_keeps_the_oldest_and_newest_witnesses_and_no_path(
    scene: Scene,
) -> None:
    repo = scene.repo("big", remote="example.invalid/acme/big")
    res = scene.see(repo)
    pid = str(res.project)
    model = scene.model
    commits = [repo.commit() for _ in range(20)]
    for commit in commits:
        model.add_witness(pid, commit, source="test")
    model.resolve(
        repo.location(scene.machine, remote_keys=frozenset({"local:/src/big"}))
    )
    identity = scene.model.portable_identity(pid)
    order = [
        str(r["commit_id"])
        for r in scene.store.membership_rows(
            "project_witnesses", where={"project": pid}, order_by=("arrival",)
        )
    ]
    assert list(identity.witnesses) == order[:8] + order[-8:]
    assert identity.remotes == ("example.invalid/acme/big",)
    raw: dict[str, Any] = identity.to_json()
    raw["remotes"] = [*raw["remotes"], "local:/elsewhere/big"]
    assert PortableIdentity.from_json(raw) == identity
    malformed: list[object] = [
        [],
        {"name": ""},
        {"name": "x", "witnesses": "abc"},
        {"name": "x", "folder": "yes"},
        {"name": "x", "parent": True},
    ]
    for bad in malformed:
        with pytest.raises(ValueError):
            PortableIdentity.from_json(bad)


def test_import_refuses_a_document_without_identities(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = scene.root / "bad.json"
    for body in (
        "not json",
        json.dumps([1, 2]),
        json.dumps({"store_id": 3, "projects": [], "homes": {}}),
        json.dumps(
            {"store_id": "s", "projects": [{"name": "a", "parent": 4}], "homes": {}}
        ),
        json.dumps({"store_id": "s", "projects": [], "homes": {"r": {"kind": "x"}}}),
        json.dumps(
            {
                "store_id": "s",
                "projects": [],
                "homes": {"r": {"kind": "memory", "project": 0}},
            }
        ),
    ):
        bad.write_text(body, encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            bm(monkeypatch, "membership", "import", str(bad))
        assert exc.value.code == 2
        assert capsys.readouterr().err.startswith("bettermemory: ")
    with pytest.raises(SystemExit):
        bm(monkeypatch, "membership", "import", str(scene.root / "missing.json"))
    assert "cannot read" in capsys.readouterr().err


def test_membership_export_to_stdout_and_its_refusals(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    res = scene.see(scene.repo("app", remote="example.invalid/acme/app"))
    rid = scene.write(res)
    bm(monkeypatch, "membership", "export")
    doc = json.loads(capsys.readouterr().out)
    assert doc["homes"] == {rid: {"kind": "memory", "project": 0}}
    assert doc["projects"][0]["remotes"] == ["example.invalid/acme/app"]
    with pytest.raises(SystemExit):
        bm(monkeypatch, "membership", "export", "-o", str(scene.root / "no" / "x.json"))
    assert "does not exist" in capsys.readouterr().err
    monkeypatch.setenv("BETTERMEMORY_DIR", str(scene.root / "empty"))
    with pytest.raises(SystemExit):
        bm(monkeypatch, "membership", "export")
    assert "no bettermemory 9 store" in capsys.readouterr().err


def test_an_imported_sub_project_keeps_its_parent(scene: Scene) -> None:
    root = scene.repo("mono")
    project_file.write(root.path, name="mono")
    root_res = scene.see(root)
    parent = PortableIdentity(
        name="mono",
        file_ids=tuple(
            str(r["file_id"]) for r in scene.store.membership_rows("project_files")
        ),
    )
    child = PortableIdentity(name="api", file_ids=("bmp_" + "A" * 26,), parent=0)
    model = scene.model
    up = model.import_identity(parent, source="mono.json")
    assert (up.project, up.rule) == (root_res.project, "import:F")
    down = model.import_identity(child, source="mono.json", parent=up.project)
    assert down.rule == "import:new:no_live_checkout"
    assert scene.model.project(down.project).parent == root_res.project
    assert scene.model.chain_of(down.project) == [down.project, root_res.project]

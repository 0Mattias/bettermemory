"""Membership security (design 3.5; design section 7 rows E12, E21, E34;
decisions D13, D17, B6, B9): what no surface delivers.

The lab is real: repositories built with git under ``tmp_path`` with a
neutral configuration, project files written where the case needs them,
and every caller resolved by `location.resolve_caller` at its directory.
Each record is written with the home its writer's resolution gives it
(`Caller.home`), and each surface is read through the reader production
uses: the brief through `Store.brief_rows` and `recall.session_brief`,
prompt recall through `Membership.admit_recall`, search, the pool
prefilter, the review strata and the link surface through `search.search`
and `handlers.search.resolve_search_pool` under the surface's
`admission.Admission`, and the links end to end through `memory_search`.

The places:

- ``root``: a repository, its own project.
- ``part`` and ``kit``: folders of ``root`` with a project file each, two
  sub-projects of it (D13).
- ``vault``: a folder of ``root`` whose project file declares a boundary,
  a sub-project that is a tenant of its own (B9).
- ``other``: another repository, another project.
- ``client``: a repository whose root file declares a boundary, a tenant
  (E34).
- ``plain``: a folder outside any repository, the ``global`` caller.
- a record written where nothing could be read: its home is the
  provisional project of an unresolved location (B6).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location
from bettermemory.admission import NO_RESOLUTION, Admission, admitted_nothing
from bettermemory.config import Config, StorageConfig
from bettermemory.handlers.search import resolve_search_pool
from bettermemory.location import Caller, resolve_caller
from bettermemory.membership import (
    DEFERRED,
    INDETERMINATE,
    LINKS,
    POOL_PREFILTER,
    PROMPT_RECALL,
    REVIEW_STRATA,
    SEARCH_AUTO_SCOPE,
    SEARCH_UNSCOPED,
    SESSION_BRIEF,
    SURFACES,
    Membership,
    Resolution,
)
from bettermemory.models import Category, MemoryLink
from bettermemory.project_file import PROJECT_FILE_NAME, mint_id
from bettermemory.recall import RecallState, session_brief
from bettermemory.search import _filter_candidates, candidate_admitted, search
from bettermemory.server import build_server
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._mcp import call_tool
from .conftest import set_git_discovery_ceiling

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

#: The word every record's body carries, so one query reaches them all.
WORD = "lanternfish"

#: The record surfaces that rank (everything but the brief and prompt
#: recall's turn admission, which read the homes another way).
_RANKED = (SEARCH_AUTO_SCOPE, SEARCH_UNSCOPED, POOL_PREFILTER, REVIEW_STRATA, LINKS)


# ---------------------------------------------------------------------------
# Repositories (shared with the other membership tests)
# ---------------------------------------------------------------------------


def neutral_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No user or system git configuration, a fixed author and committer,
    no inherited repository variables, git's upward walk stopped above
    `tmp_path`."""
    config = tmp_path / "gitconfig"
    if not config.exists():
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
    set_git_discovery_ceiling(tmp_path, monkeypatch)


#: A fixed identity and no signing, so a commit needs no configuration.
_GIT_IDENTITY = (
    "-c",
    "user.name=Fixture",
    "-c",
    "user.email=fixture@example.invalid",
    "-c",
    "commit.gpgsign=false",
)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *_GIT_IDENTITY, *args], cwd=cwd, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def project_file(name: str, *, boundary: bool = False) -> str:
    """The text of a `bettermemory.toml` binding a fresh project id."""
    body = f'[project]\nid = "{mint_id()}"\nname = "{name}"\n'
    if boundary:
        body += "boundary = true\n"
    return body


def checkout(
    path: Path,
    *,
    files: Mapping[str, str] | None = None,
    remote: str | None = None,
) -> Path:
    """A repository at `path` with one commit of its own (a random line
    and its own message, so no two repositories share a commit or read as
    each other's rewrite) holding `files` (relative path to text), with
    `remote` as ``origin`` when given. Returns its root as git names it.
    The commit carries a fixed identity of its own; `neutral_git` sets the
    rest of the environment where a test needs it."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    token = os.urandom(6).hex()
    (path / "README.md").write_text(f"{path.name} {token}\n")
    for rel, text in (files or {}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", f"{path.name} {token}")
    if remote is not None:
        _git(path, "remote", "add", "origin", remote)
    return Path(_git(path, "rev-parse", "--show-toplevel"))


def stand_in_checkout(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    files: Mapping[str, str] | None = None,
    remote: str | None = None,
) -> Path:
    """Make the process cwd a fresh repository under `base` (a directory
    of its own, `tmp_path_factory.mktemp`), with the git environment
    `neutral_git` sets: a caller that names no directory then resolves to
    that checkout's project (design 3.1), where the automatic surfaces
    deliver its records (design 3.5)."""
    neutral_git(base, monkeypatch)
    repo = checkout(base / "checkout", files=files, remote=remote)
    monkeypatch.chdir(repo)
    return repo


# ---------------------------------------------------------------------------
# The lab
# ---------------------------------------------------------------------------


class Lab:
    """The places, a store, and one record written at each place."""

    def __init__(self, tmp_path: Path) -> None:
        self.store_dir = tmp_path / "store"
        self.store = Store(self.store_dir)
        self.config = Config(storage=StorageConfig(directory=str(self.store_dir)))
        root = checkout(
            tmp_path / "root",
            files={
                f"part/{PROJECT_FILE_NAME}": project_file("part"),
                f"kit/{PROJECT_FILE_NAME}": project_file("kit"),
                f"vault/{PROJECT_FILE_NAME}": project_file("vault", boundary=True),
            },
        )
        plain = tmp_path / "plain"
        plain.mkdir()
        self.places: dict[str, Path] = {
            "root": root,
            "part": root / "part",
            "kit": root / "kit",
            "vault": root / "vault",
            "other": checkout(tmp_path / "other"),
            "client": checkout(
                tmp_path / "client",
                files={PROJECT_FILE_NAME: project_file("client", boundary=True)},
            ),
            "plain": plain,
        }
        self.callers: dict[str, Caller] = {
            name: self.resolve(path) for name, path in self.places.items()
        }
        self.records: dict[str, str] = {
            name: self.write(name, f"{WORD} note written at {name}")
            for name in self.places
        }
        nowhere = resolve_caller(self.store, self.config, None)
        assert nowhere.resolution.outcome == INDETERMINATE
        self.records["unresolved"] = self._write(
            nowhere, f"{WORD} note written where nothing could be read"
        )

    def resolve(self, path: Path | None) -> Caller:
        return resolve_caller(
            self.store, self.config, None if path is None else str(path)
        )

    def caller(self, name: str) -> Resolution:
        return self.callers[name].resolution

    def write(
        self, place: str, body: str, *, links: list[MemoryLink] | None = None
    ) -> str:
        return self._write(self.resolve(self.places[place]), body, links=links)

    def _write(
        self, caller: Caller, body: str, *, links: list[MemoryLink] | None = None
    ) -> str:
        memory = self.store.write(
            content=body, scopes=["tools"], links=links, home=caller.home()
        )
        self.store.mark_verified(memory.id)
        return memory.id

    def model(self) -> Membership:
        return self.callers["root"].model

    def admission(self, surface: str, who: str | Resolution | None) -> Admission:
        caller = self.caller(who) if isinstance(who, str) else who
        return Admission(surface, caller, None if caller is None else self.model())

    def names(self, ids: Iterable[str]) -> set[str]:
        """The places of the lab's records among `ids`."""
        by_id = {rid: name for name, rid in self.records.items()}
        return {by_id[rid] for rid in ids if rid in by_id}

    # -- the surfaces ---------------------------------------------------------

    def brief(self, who: str | Resolution | None) -> set[str]:
        """What the session brief's reader admits."""
        admission = self.admission(SESSION_BRIEF, who)
        if admission.nothing is not None:
            return set()
        return self.names(
            self.store.brief_rows(
                admit=admission.admits_row,
                provenance=("local", "imported"),
                exclude_category=Category.AMBIENT.value,
            )
        )

    def recall(self, who: str | Resolution | None) -> set[str]:
        """What prompt recall delivers of every record, each relevant: the
        pool under PROMPT_RECALL, then the turn's admission."""
        admission = self.admission(PROMPT_RECALL, who)
        pool = self.ranked(PROMPT_RECALL, who)
        caller = admission.caller
        if caller is None or admission.model is None:
            return pool
        ids = [self.records[name] for name in sorted(pool)]
        turn = admission.model.admit_recall(caller, list(ids))
        return self.names(turn.delivered)

    def ranked(self, surface: str, who: str | Resolution | None) -> set[str]:
        """What `search.search` ranks under the surface's admission."""
        hits = search(
            self.store.load_all(),
            WORD,
            admission=self.admission(surface, who),
            max_results=50,
        )
        return self.names(hit.id for hit in hits)

    def pool(self, surface: str, who: str | Resolution | None) -> set[str]:
        """The candidate pool production retrieval draws under the
        surface's admission."""
        drawn = resolve_search_pool(
            self.store, WORD, admission=self.admission(surface, who), min_survivors=5
        )
        admission = self.admission(surface, who)
        return self.names(
            m.id
            for m in _filter_candidates(
                drawn.memories, scopes=None, excluded_scopes=None, admission=admission
            )
        )

    def every_surface(self, who: str | Resolution | None) -> dict[str, set[str]]:
        out = {surface: self.ranked(surface, who) for surface in _RANKED}
        out[SESSION_BRIEF] = self.brief(who)
        out[PROMPT_RECALL] = self.recall(who)
        out["pool:" + POOL_PREFILTER] = self.pool(POOL_PREFILTER, who)
        out["pool:" + SEARCH_UNSCOPED] = self.pool(SEARCH_UNSCOPED, who)
        return out


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Lab]:
    neutral_git(tmp_path, monkeypatch)
    made = Lab(tmp_path)
    yield made
    made.store.close()


def test_the_lab_resolves_as_the_cases_need(lab: Lab) -> None:
    """The places stand where the cases say: sub-projects on the root's
    chain, the boundaries tenants, the plain folder outside any project."""
    root = lab.caller("root")
    assert root.project is not None and root.tenant is None
    for part in ("part", "kit"):
        res = lab.caller(part)
        assert res.chain[1:] == (root.project,)
        assert res.tenant is None
    vault = lab.caller("vault")
    assert root.project in vault.chain and vault.tenant is not None
    client = lab.caller("client")
    assert client.tenant is not None and client.tenant != vault.tenant
    assert lab.caller("plain").project == "global"
    assert len({lab.caller(n).project for n in lab.places}) == len(lab.places)


# ---------------------------------------------------------------------------
# E34 d: a tenant's records cross no boundary, on any surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["root", "part", "kit", "other", "plain"])
def test_e34_a_tenants_record_reaches_no_caller_outside_it(lab: Lab, who: str) -> None:
    """E34 d: the client's record (a tenant) and the vault's (a boundary
    sub-project, B9) reach no caller on the other side of their boundary,
    scoped or unscoped, ranked or pooled."""
    for surface, delivered in lab.every_surface(who).items():
        assert "client" not in delivered, (who, surface)
        assert "vault" not in delivered, (who, surface)


@pytest.mark.parametrize("tenant", ["client", "vault"])
def test_e34_a_tenants_caller_meets_no_record_outside_it(lab: Lab, tenant: str) -> None:
    """E34 d, the other way: a tenant's caller is delivered its own records
    and records written outside any project, nothing of another project,
    unscoped included."""
    allowed = {tenant, "plain"}
    for surface, delivered in lab.every_surface(tenant).items():
        assert delivered <= allowed, (surface, delivered)
    assert tenant in lab.ranked(SEARCH_UNSCOPED, tenant)
    assert tenant in lab.brief(tenant)


def test_e34_the_unscoped_search_reaches_other_projects_but_no_tenant(
    lab: Lab,
) -> None:
    """E34 d: the unscoped search widens to other projects of the caller's
    side of every boundary, never past one."""
    assert lab.ranked(SEARCH_UNSCOPED, "root") == {
        "root",
        "part",
        "kit",
        "other",
        "plain",
    }
    assert lab.ranked(SEARCH_UNSCOPED, "plain") == {
        "root",
        "part",
        "kit",
        "other",
        "plain",
    }


# ---------------------------------------------------------------------------
# B6: a record whose home is unresolved reaches no caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "who", ["root", "part", "kit", "vault", "other", "client", "plain"]
)
def test_b6_an_unresolved_home_reaches_no_caller(lab: Lab, who: str) -> None:
    """B6: a record written where nothing could be read lands in a
    provisional project of its own, which no surface delivers to any
    caller until a merge or a declaration settles it."""
    home = lab.admission(SESSION_BRIEF, who).home(lab.records["unresolved"])
    assert home is not None and home.unresolved
    for surface, delivered in lab.every_surface(who).items():
        assert "unresolved" not in delivered, (who, surface)


# ---------------------------------------------------------------------------
# E21: a caller resolved to nothing is delivered nothing, and says so
# ---------------------------------------------------------------------------


def _nothing_callers(lab: Lab) -> dict[str, Resolution | None]:
    unread = lab.resolve(None).resolution
    assert unread.outcome == INDETERMINATE and unread.project is None
    deferred = dataclasses.replace(lab.caller("root"), outcome=DEFERRED, project=None)
    return {INDETERMINATE: unread, DEFERRED: deferred, NO_RESOLUTION: None}


def test_e21_a_caller_resolved_to_nothing_is_delivered_nothing(lab: Lab) -> None:
    """E21: INDETERMINATE, DEFERRED and a resolution that raised are
    admitted nothing on every surface; the origin columns never stand
    in."""
    for outcome, res in _nothing_callers(lab).items():
        assert lab.admission(SESSION_BRIEF, res).nothing == outcome
        for surface in SURFACES:
            admission = lab.admission(surface, res)
            assert admission.nothing == outcome
            assert admission.admitted_projects() is None
            assert not admission.admits_global()
            assert admission.admitted_ids(list(lab.records.values())) == []
        for surface, delivered in lab.every_surface(res).items():
            assert delivered == set(), (outcome, surface)


def test_e21_the_brief_row_says_the_caller_was_admitted_nothing(lab: Lab) -> None:
    """E21: the brief of a caller nothing could be read for, or whose read
    is deferred, points at no record (the resolution's own line says
    nothing from any project is shown) and its `session_brief` row says
    why, with no ids."""
    callers = _nothing_callers(lab)
    for n, outcome in enumerate((INDETERMINATE, DEFERRED)):
        result = session_brief(
            session_id=f"nothing-{n}",
            config=lab.config,
            store=lab.store,
            state=RecallState(background=False),
            resolution=callers[outcome],
            cwd=lab.places["root"],
        )
        assert result.ids == ()
        assert not any(rid in result.stdout for rid in lab.records.values())
        (row,) = [
            e
            for e in lab.store.iter_events()
            if e["kind"] == "session_brief" and e["session"] == f"nothing-{n}"
        ]
        assert row["ids"] == []
        assert row["membership"] == admitted_nothing(outcome)


def test_e21_a_brief_whose_resolution_raised_says_so(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E21: where the caller's resolution raised, the brief shows nothing
    and its row names ``no_resolution``."""

    def raises(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the ladder failed")

    monkeypatch.setattr(location, "resolve_hook", raises)
    result = session_brief(
        session_id="raised",
        config=lab.config,
        store=lab.store,
        state=RecallState(background=False),
        cwd=lab.places["root"],
    )
    assert result.stdout == "" and result.ids == ()
    (row,) = [
        e
        for e in lab.store.iter_events()
        if e["kind"] == "session_brief" and e["session"] == "raised"
    ]
    assert row["membership"] == admitted_nothing(NO_RESOLUTION)


async def test_e21_a_search_by_a_caller_admitted_nothing_says_so(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E21: `memory_search` for a caller that nothing could be read for
    returns no record, scoped or unscoped, and its search row says so."""
    from bettermemory.handlers import search as search_handler
    from bettermemory.verify import Anchoring

    unread = lab.resolve(None)

    def anchored(store: Store, config: Config) -> tuple[Anchoring, Caller]:
        return Anchoring(unread.model, unread.resolution), unread

    monkeypatch.setattr(search_handler, "caller_anchoring", anchored)
    server = build_server(config=lab.config, store=lab.store, state=SessionState())
    for auto_scope in (True, False):
        res = await call_tool(
            server, "memory_search", {"query": WORD, "auto_scope": auto_scope}
        )
        hits = res.get("result", res) if isinstance(res, dict) else res
        assert hits == [] or not any(
            h.get("id") in lab.records.values() for h in hits
        ), hits
    rows = [e for e in lab.store.iter_events() if e["kind"] == "search"]
    assert len(rows) == 2
    for row in rows:
        assert row["membership"] == admitted_nothing(INDETERMINATE)


# ---------------------------------------------------------------------------
# D13 and B9: sub-projects share with the root, except across a boundary
# ---------------------------------------------------------------------------


def test_d13_a_sub_projects_session_gets_its_own_and_its_parents(lab: Lab) -> None:
    """D13 A: a session in a sub-project gets its own records and the
    root's, never a sibling's; the vault's boundary keeps it apart (B9)."""
    assert lab.brief("part") == {"part", "root"}
    assert lab.recall("part") == {"part", "root"}
    assert lab.ranked(SEARCH_AUTO_SCOPE, "part") == {"part", "root", "plain"}
    assert lab.ranked(REVIEW_STRATA, "part") == {"part", "root"}
    assert "kit" not in lab.ranked(SEARCH_AUTO_SCOPE, "part")


def test_d13_the_roots_search_reaches_its_sub_projects(lab: Lab) -> None:
    """D13 A: the root's search reaches the sub-projects' records; its
    session brief and recall keep to its own (design 3.5), and the
    boundary sub-project stays apart on every surface (B9)."""
    assert lab.ranked(SEARCH_AUTO_SCOPE, "root") == {"root", "part", "kit", "plain"}
    assert lab.pool(POOL_PREFILTER, "root") == {"root", "part", "kit", "plain"}
    assert lab.ranked(LINKS, "root") == {"root", "part", "kit", "plain"}
    assert lab.brief("root") == {"root"}
    assert lab.recall("root") == {"root"}


def test_b9_a_boundary_sub_project_shares_nothing_with_its_root(lab: Lab) -> None:
    """B9: a boundary outranks D13's sharing: the vault's records do not
    reach the root's sessions, nor the root's reach the vault."""
    for surface, delivered in lab.every_surface("root").items():
        assert "vault" not in delivered, surface
    for surface, delivered in lab.every_surface("vault").items():
        assert delivered <= {"vault", "plain"}, (surface, delivered)
    assert lab.brief("vault") == {"vault"}


# ---------------------------------------------------------------------------
# The link surface omits a target it does not admit
# ---------------------------------------------------------------------------


async def test_links_omit_a_target_the_caller_is_not_admitted_to(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit in the root whose superseding, contradicting and depended-on
    records sit in the client (a tenant) shows none of them; the same
    links from a global record show (LINKS admits ``global``)."""
    target = lab.write("root", "the lanternfish deploy window is tuesday")
    tenant_links = [
        MemoryLink(type="supersedes", target_id=target),
        MemoryLink(type="contradicts", target_id=target),
    ]
    hidden = lab.write("client", "a tenant's private replacement", links=tenant_links)
    shown = lab.write(
        "plain",
        "a global replacement",
        links=[MemoryLink(type="supersedes", target_id=target)],
    )
    needs_hidden = lab.write(
        "root",
        "the lanternfish rollout checklist",
        links=[
            MemoryLink(type="depends_on", target_id=hidden),
            MemoryLink(type="depends_on", target_id=shown),
        ],
    )
    monkeypatch.chdir(lab.places["root"])
    server = build_server(config=lab.config, store=lab.store, state=SessionState())
    for auto_scope in (True, False):
        res = await call_tool(
            server,
            "memory_search",
            {"query": "lanternfish deploy rollout", "auto_scope": auto_scope},
        )
        hits = res.get("result", res) if isinstance(res, dict) else res
        by_id = {h["id"]: h for h in hits}
        assert hidden not in by_id
        hit = by_id[target]
        assert [e["id"] for e in hit.get("superseded_by", [])] == [shown]
        assert hidden not in [e["id"] for e in hit.get("contradicts", [])]
        resolved = [e["id"] for e in by_id[needs_hidden].get("depends_on_resolved", [])]
        assert hidden not in resolved
        assert shown in resolved


# ---------------------------------------------------------------------------
# Parity: the IDF denominator is the ranked set under every surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", sorted(SURFACES))
@pytest.mark.parametrize(
    "who", ["root", "part", "vault", "other", "client", "plain", "nothing"]
)
def test_the_idf_denominator_is_the_ranked_set(
    lab: Lab, surface: str, who: str
) -> None:
    """The document frequencies' collection (`Store.document_frequencies`
    under the search's own predicate, `candidate_admitted`) is exactly the
    set `_filter_candidates` ranks, for every surface and caller, a caller
    admitted nothing included."""
    caller = None if who == "nothing" else lab.caller(who)
    admission = lab.admission(surface, caller)
    ranked = _filter_candidates(
        lab.store.load_all(), scopes=None, excluded_scopes=None, admission=admission
    )

    def admit(scopes: list[str], home: str | None, actor: Any) -> bool:
        return candidate_admitted(
            scopes, home, actor, scope_filter=None, excluded=set(), admission=admission
        )

    for memoised in (False, True):
        stats = lab.store.document_frequencies([WORD], admit=admit, memoised=memoised)
        size = 0 if stats is None else stats[0]
        assert size == len(ranked), (surface, who, memoised)
        if stats is not None:
            assert stats[1].get(WORD, 0) == len(ranked)


# ---------------------------------------------------------------------------
# RS-F7 part 2: a global record admitted by the project its scopes name
# ---------------------------------------------------------------------------


def test_rs_f7_a_global_record_naming_a_project_reaches_it(lab: Lab) -> None:
    """A record written outside any project whose scopes name a project
    reaches that project's callers on the brief, the prompt recall and the
    review's strata, as it does on the scoped surfaces; the plain record,
    naming none, reaches none of those three; a project the scopes do not
    name meets neither on them."""
    plain = lab.resolve(lab.places["plain"])
    named = lab.store.write(
        content=f"{WORD} note naming other",
        scopes=["projects:other"],
        home=plain.home(),
    )
    lab.store.mark_verified(named.id)
    lab.records["named"] = named.id
    automatic = (SESSION_BRIEF, PROMPT_RECALL, REVIEW_STRATA)
    seen = lab.every_surface("other")
    for surface in automatic:
        assert "named" in seen[surface], surface
        assert "plain" not in seen[surface], surface
    for surface in (SEARCH_AUTO_SCOPE, LINKS):
        assert {"named", "plain"} <= seen[surface], surface
    elsewhere = lab.every_surface("root")
    for surface in automatic:
        assert not {"named", "plain"} & elsewhere[surface], surface

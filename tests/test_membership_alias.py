"""An alias clone and its full-URL clone are one project (ENG-207).

Before 9.0.0rc14 a remote spelled with an ``insteadOf`` alias keyed as
written, so a clone of ``gh:acme/lib`` and a clone of
``https://github.com/acme/lib`` could be kept as two projects. Since rc14
the alias clone's sightings record the full URL's key on its project too,
and a third clone then named both projects with two witnesses each: the
ladder asked. A sighting of the alias clone now merges its project into
the one project that fetches from the same remote and whose commits it
holds, where nothing a user declared or a file binds stands against it
(`membership_alias`), and a third clone asked between the two re-tallies
and joins. Each condition of the rule has a test here that fails without
it. The model's locations are built by hand (`tests/test_membership_model.py`'s
world), the alias keys as `location_git.read_remotes` reports them; the
issue's scenario, the repository's own rule and the cost run on git.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from datetime import timedelta
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bettermemory import location, membership_alias
from bettermemory.config import Config
from bettermemory.location import resolve_here
from bettermemory.machine import MachineIdentity
from bettermemory.membership import ASK, GLOBAL, NEW, SILENT, Declaration, Resolution
from bettermemory.membership_records import (
    FIRST_SIGHT,
    IMPORT,
    LEGACY,
    RETIRE_AFTER,
    UNREADABLE,
    Location,
    ProjectFileRef,
)
from bettermemory.membership_shared import _SUBJECT
from bettermemory.store import Store
from bettermemory.time_utils import isoformat_utc

from .test_membership_cost import (
    _BASE_STATS,
    _Counts,
    counted,
    git,
    lab,
    make_repo,
    neutral_git,
)
from .test_membership_model import _OPEN_STORES, Repo, World, _client, _file

__all__ = ["counted", "lab", "neutral_git"]

#: The tests that run git.
_ON_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

#: The folder the checkouts lie in, absolute for the platform (a drive on
#: Windows) as a capture records a view, so a declared folder, which the
#: model makes so (`membership.view_path`), names the same one.
_W = os.path.join(os.path.abspath(os.sep), "w")


def _w(name: str) -> str:
    """The folder `name` in `_W`."""
    return os.path.join(_W, name)


ALIAS = "gh/acme/lib"
FULL = "github.com/acme/lib"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    yield World(tmp_path)
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


def _alias_sighting(
    world: World, repo: Repo, aliases: dict[str, str], *, strict: bool = False
) -> Resolution:
    """A sighting of `repo` as the capture keys it since rc14: each alias
    key as written and as git fetches it, the alias keys named."""
    keys = (repo.remotes - aliases.keys()) | set(aliases) | set(aliases.values())
    return world.see(
        repo,
        strict=strict,
        remote_keys=frozenset(keys),
        alias_keys=frozenset(aliases),
    )


def _split_pair(world: World) -> tuple[Repo, Repo, Resolution, Resolution]:
    """The full-URL clone and the alias clone as two new projects, as a
    store before rc14 kept them: the full-URL clone seen first, ahead of
    the alias clone, so the alias clone held none of its witness commits
    and shared no remote key with it."""
    return _split_pair_with(world)


def _split_pair_with(
    world: World, *, alias_files: tuple[ProjectFileRef, ...] = ()
) -> tuple[Repo, Repo, Resolution, Resolution]:
    """`_split_pair`, the alias clone first seen carrying `alias_files`."""
    full = world.repo(_w("full"), remote=FULL)
    alias = world.clone(full, _w("alias"), remote=ALIAS, files=alias_files)
    full.commit()
    first = world.see(full)
    second = world.see(alias)
    assert (first.outcome, second.outcome) == (NEW, NEW)
    assert first.project != second.project
    return full, alias, first, second


def _caught_up(full: Repo, alias: Repo) -> None:
    """The alias clone fetched: it holds the full-URL clone's commits."""
    alias.history = full.history.copy()
    alias.head = full.head


def _merges(world: World) -> list[str]:
    return [r.payload for r in world.store.log_rows() if r.kind == "project_merged"]


def _reads_in_the_transaction(world: World) -> list[str]:
    """Each view whose liveness the world's model reads from now on while
    the store's write transaction is open, by path."""
    seen: list[str] = []
    real = world.model._liveness

    def spy(checkout: Any, view: Any) -> str:
        if world.store.in_batch:
            seen.append(view.path)
        return real(checkout, view)

    world.model._liveness = spy
    return seen


def _stays_apart(world: World, alias: Repo, second: Resolution) -> None:
    for _ in range(2):
        assert _alias_sighting(world, alias, {ALIAS: FULL}).project == second.project
    assert _merges(world) == []


# ---------------------------------------------------------------------------
# The join
# ---------------------------------------------------------------------------


def test_an_alias_clone_and_its_full_url_clone_resolve_to_one_project(
    world: World,
) -> None:
    """The alias clone fetches and is seen as rc14 keys it: its project
    joins the full-URL clone's, which stands; both clones resolve there,
    and a third clone of either spelling joins it without a question. The
    line names the project merged and its undo."""
    full, alias, first, second = _split_pair(world)
    _caught_up(full, alias)
    seen = _alias_sighting(world, alias, {ALIAS: FULL})
    assert (seen.outcome, seen.project) == (SILENT, first.project)
    (line,) = [x for x in seen.lines if "joined it" in x]
    assert str(second.project) in line
    assert f'memory_admin(action="unjoin_project", id="{second.project}")' in line
    merged = _merges(world)
    assert len(merged) == 1 and str(second.project) in merged[0]
    assert ALIAS in merged[0]
    assert world.see(full).project == first.project
    assert _alias_sighting(world, alias, {ALIAS: FULL}).project == first.project
    third = world.clone(full, _w("third"), remote=FULL)
    res = world.see(third)
    assert (res.outcome, res.project, res.rule) == (SILENT, first.project, "join:H+R")
    fourth = world.clone(full, _w("fourth"), remote=ALIAS)
    res = _alias_sighting(world, fourth, {ALIAS: FULL})
    assert (res.outcome, res.project) == (SILENT, first.project)
    assert len(_merges(world)) == 1


def test_a_third_clone_asked_before_the_join_joins_without_asking_again(
    world: World,
) -> None:
    """The review's P7b: a store already asking, as rc14 left it. The
    alias clone's project and the full-URL clone's both hold the full
    URL's key and its commits, and a third clone's question names both
    (its own provisional project holds the key too, and is not counted).
    The alias clone's sighting joins the two; the third clone's next
    sighting re-tallies the question whose candidates are now one, and
    joins with no question left."""
    full, alias, first, second = _split_pair(world)
    _caught_up(full, alias)
    _alias_sighting(world, alias, {ALIAS: FULL}, strict=True)  # as rc14 saw it
    third = world.clone(full, _w("third"), remote=FULL)
    asked = world.see(third)
    assert asked.outcome == ASK
    assert {str(c) for c in world.model.proposal(str(asked.proposal)).candidates} == {
        str(first.project),
        str(second.project),
    }
    assert _alias_sighting(world, alias, {ALIAS: FULL}).project == first.project
    again = world.see(third)
    assert (again.outcome, again.project) == (SILENT, first.project)
    assert world.model.pending() == []
    assert world.see(third).outcome == SILENT


def test_an_alias_clone_behind_its_full_url_clone_waits_for_its_commits(
    world: World,
) -> None:
    """Holding none of the other project's witness commits (H), the alias
    clone's remote alone is one witness: nothing is joined until it
    fetches them."""
    full, alias, first, second = _split_pair(world)
    _stays_apart(world, alias, second)
    _caught_up(full, alias)
    assert _alias_sighting(world, alias, {ALIAS: FULL}).project == first.project


# ---------------------------------------------------------------------------
# The remote, read on the URL git fetches
# ---------------------------------------------------------------------------


def test_repositories_sharing_only_an_alias_spelling_stay_apart(
    world: World,
) -> None:
    """Two machines' rules give ``gh:lib`` two meanings: a fork and its
    upstream, the fork holding the upstream's commits, both come to record
    the alias key ``gh/lib``. The remote is read on the URL each fetches,
    which the other does not hold, so neither joins the other; nor do two
    repositories whose aliases share only a prefix."""
    upstream = world.repo(_w("upstream"), remote="github.com/acme/lib")
    fork = world.clone(upstream, _w("fork"), remote="github.com/other/lib")
    fork.commit()
    forked = world.see(fork)
    up = world.see(upstream)
    assert (forked.outcome, up.outcome) == (NEW, NEW)
    upstream.remotes = fork.remotes = {"gh/lib"}
    for _ in range(2):
        a = _alias_sighting(world, upstream, {"gh/lib": "github.com/acme/lib"})
        b = _alias_sighting(world, fork, {"gh/lib": "github.com/other/lib"})
        assert a.project == up.project and b.project == forked.project
    lib = world.repo(_w("lib"), remote="gh/acme/lib")
    app = world.repo(_w("app"), remote="gh/acme/app")
    lib_aliases = {"gh/acme/lib": "github.com/acme/lib"}
    app_aliases = {"gh/acme/app": "github.com/acme/app"}
    seen_lib = _alias_sighting(world, lib, lib_aliases)
    seen_app = _alias_sighting(world, app, app_aliases)
    for _ in range(2):
        assert _alias_sighting(world, lib, lib_aliases).project == seen_lib.project
        assert _alias_sighting(world, app, app_aliases).project == seen_app.project
    assert seen_lib.project != seen_app.project
    assert _merges(world) == []


def test_a_fork_whose_own_remote_is_an_alias_is_not_joined_to_its_upstream(
    world: World,
) -> None:
    """A fork's checkout naming its upstream by an alias fetches from a
    remote the upstream's project does not hold (its own): it stays its
    own project."""
    full, fork, first, second = _split_pair(world)
    fork.remotes = {"gh/me/lib", ALIAS}
    _caught_up(full, fork)
    fork.commit()
    aliases = {"gh/me/lib": "github.com/me/lib", ALIAS: FULL}
    for _ in range(2):
        assert _alias_sighting(world, fork, aliases).project == second.project
    assert _merges(world) == []


@pytest.mark.parametrize("spelling", ["alias", "full URL"])
def test_a_project_re_pointed_at_the_url_stays_apart_under_either_spelling(
    world: World, spelling: str
) -> None:
    """The review's P9: a settled project minted on a remote of its own,
    its checkout re-pointed at the full-URL clone's repository and its
    commits fetched. Spelled as the URL or as an alias the machine
    expands to it, the project stays apart: only a project minted on the
    alias as written is the split the rule repairs, so a remote's
    spelling decides no merge."""
    full = world.repo(_w("full"), remote=FULL)
    mine = world.clone(full, _w("mine"), remote="github.com/me/mine")
    full.commit()
    first = world.see(full)
    second = world.see(mine)
    assert (first.outcome, second.outcome) == (NEW, NEW)
    assert first.project != second.project
    _caught_up(full, mine)
    mine.remotes = {ALIAS if spelling == "alias" else FULL}
    for _ in range(2):
        res = (
            _alias_sighting(world, mine, {ALIAS: FULL})
            if spelling == "alias"
            else world.see(mine)
        )
        assert res.project == second.project
    assert _merges(world) == []


def test_a_project_re_pointed_at_the_alias_before_a_rule_names_it_stays_apart(
    world: World,
) -> None:
    """P9 in two steps: the settled project's checkout re-pointed at the
    alias while no rule names it, so a sighting records the alias as
    written alone, and the rule added after. The project was minted on
    its own remote, not on the alias: it stays apart."""
    full = world.repo(_w("full"), remote=FULL)
    mine = world.clone(full, _w("mine"), remote="github.com/me/mine")
    full.commit()
    first = world.see(full)
    second = world.see(mine)
    assert (first.outcome, second.outcome) == (NEW, NEW)
    _caught_up(full, mine)
    mine.remotes = {ALIAS}
    assert world.see(mine).project == second.project
    _stays_apart(world, mine, second)


def test_a_project_minted_on_the_alias_expanded_stays_apart(world: World) -> None:
    """The alias clone first seen as rc14 keys it, the alias expanded,
    before the full-URL clone's project fetched from the URL: its project
    was minted on the URL's key too, so no alias kept as written split
    it, and once the other project fetches from the URL a sighting's
    evidence merges neither."""
    full = world.repo(_w("full"))
    alias = world.clone(full, _w("alias"), remote=ALIAS)
    full.commit()
    first = world.see(full)
    second = _alias_sighting(world, alias, {ALIAS: FULL})
    assert (first.outcome, second.outcome) == (NEW, NEW)
    full.remotes = {FULL}
    assert world.see(full).project == first.project
    _caught_up(full, alias)
    _stays_apart(world, alias, second)


def _log_reads(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, int, int]]:
    """Each read of the log's ``project_minted`` or ``checkout_put`` rows
    (`membership_alias._first_remotes`' alone on a sighting's path), as
    the kind, the sequence number read after, and the rows taken."""
    reads: list[tuple[str, int, int]] = []
    real = world.store.membership_log

    def spy(kinds: Any = None, *, since_seq: int = 0) -> Iterator[Any]:
        rows = real(kinds, since_seq=since_seq)
        if kinds is None or list(kinds) not in (["project_minted"], ["checkout_put"]):
            return rows
        reads.append((str(kinds[0]), since_seq, 0))
        at = len(reads) - 1

        def counted_rows() -> Iterator[Any]:
            for row in rows:
                kind, since, taken = reads[at]
                reads[at] = (kind, since, taken + 1)
                yield row

        return counted_rows()

    monkeypatch.setattr(world.store, "membership_log", spy)
    return reads


def _minted_with_no_checkout(world: World, repo: Repo) -> str:
    """A project minted as a migration or an import mints one, with no
    checkout: the remote spelled with the alias, as a store before rc14
    held it, and `repo`'s HEAD its witness."""
    model = world.model
    with model._call():
        p = model._mint_project("lib", reason="import:document")
        model._add_remotes(p.id, [ALIAS])
        assert repo.head is not None
        model._add_witness(p.id, repo.head, source="import:document", history=None)
    return p.id


def test_a_projects_first_checkout_row_is_read_once_for_the_open_store(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's residual 4: each judgment read the log from the
    project's mint to its first checkout row, once for each HEAD in each
    process. The row found is kept for the open store and the project: a
    second judgment, at a new HEAD, reads no log row. Another store, whose
    project of the same id has another first row, reads its own."""
    full = world.repo(_w("full"), remote=FULL)
    mine = world.clone(full, _w("mine"), remote="github.com/me/mine")
    full.commit()
    world.see(full)
    second = world.see(mine)
    _caught_up(full, mine)
    mine.remotes = {ALIAS}
    reads = _log_reads(world, monkeypatch)
    assert _alias_sighting(world, mine, {ALIAS: FULL}).project == second.project
    assert [kind for kind, _since, _taken in reads] == [
        "project_minted",
        "checkout_put",
    ]
    reads.clear()
    mine.commit()
    assert _alias_sighting(world, mine, {ALIAS: FULL}).project == second.project
    assert reads == []
    assert _merges(world) == []

    other = World(tmp_path / "other")
    *_, split = _split_pair(other)
    assert split.project == second.project
    pid = str(second.project)
    assert membership_alias._first_remotes(other.model, pid) == frozenset({ALIAS})
    assert membership_alias._first_remotes(world.model, pid) == frozenset(
        {"github.com/me/mine"}
    )


class _RolledBack(Exception):
    """Raised inside a batch to roll it back."""


@pytest.mark.parametrize("found", [True, False])
def test_what_is_read_in_a_transaction_rolled_back_is_not_kept(
    world: World, found: bool
) -> None:
    """The review's P12: a judgment that read a project's first checkout
    row inside a transaction that then rolled back kept the row, and
    project ids are the highest plus one, so the next project minted took
    the id with another first row, and the memo answered that it was
    minted on the alias. A read finding none yet kept a read position past
    sequence numbers the next writes took again, and passed over their
    rows. Nothing read past the last row committed is kept
    (`Store.committed_seq`): the next judgment reads the log the rollback
    left."""
    alias = world.repo(_w("alias"), remote=ALIAS)
    alias.commit()
    mine = world.repo(_w("mine"), remote="github.com/me/mine")
    mine.commit()
    store = world.store
    head = max((row.seq for row in store.log_rows()), default=0)
    with pytest.raises(_RolledBack), store.batch():
        assert store.committed_seq() is None
        if found:
            pid = str(world.see(alias).project)
            assert membership_alias._first_remotes(world.model, pid) == frozenset(
                {ALIAS}
            )
            assert membership_alias._minted_on(world.model, pid, [ALIAS], [FULL])
        else:
            pid = _minted_with_no_checkout(world, alias)
            assert membership_alias._first_remotes(world.model, pid) is None
        assert store.committed_seq() == head
        raise _RolledBack
    assert store.committed_seq() is None
    # What is kept holds no row and reads no further than the commits did.
    memo = membership_alias._FIRST.get(store)
    kept = None if memo is None else memo.get((store.store_id, pid))
    assert kept is None or (kept[0], kept[2]) == (None, None) and kept[1] <= head
    assert str(world.see(mine).project) == pid, "premise: the id is taken again"
    assert membership_alias._first_remotes(world.model, pid) == frozenset(
        {"github.com/me/mine"}
    )
    assert not membership_alias._minted_on(world.model, pid, [ALIAS], [FULL])


def test_only_what_is_committed_of_a_first_row_read_is_kept() -> None:
    """`membership_alias._committed`: of a read's mint, read position and
    first row, a mint or a row past the last committed is dropped and the
    read position stops there; with every row committed all is kept, and
    a commit point before where the read began keeps nothing."""
    first = frozenset({ALIAS})
    kept = membership_alias._committed
    assert kept(None, 0, (3, 7, first)) == (3, 7, first)
    assert kept(9, 0, (3, 7, first)) == (3, 7, first)
    assert kept(7, 0, (3, 7, first)) == (3, 7, first)
    assert kept(5, 0, (3, 7, first)) == (3, 5, None)
    assert kept(2, 0, (3, 7, first)) == (None, 2, None)
    assert kept(5, 0, (None, 8, None)) == (None, 5, None)
    assert kept(5, 0, (3, 4, None)) == (3, 4, None)
    assert kept(3, 4, (None, 8, None)) is None


def test_a_project_minted_with_no_checkout_is_judged_by_the_first_to_join_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project a migration or an import minted with no checkout, on the
    alias as written: the full-URL clone's checkout row comes between its
    mint and the first row naming it, the alias clone's as it joined, and
    names the project's id too, as its branch. The row naming the project
    is judged, never another checkout's (the review's N2), and the
    project merges. None found yet is kept as how far the log was
    read, not as an answer, since a checkout row can come later: a
    judgment before the clone joined reads the rows written since."""
    full = world.repo(_w("full"), remote=FULL)
    alias = world.clone(full, _w("alias"), remote=ALIAS)
    alias.commit()
    full.commit()
    pid = _minted_with_no_checkout(world, alias)
    reads = _log_reads(world, monkeypatch)
    assert membership_alias._first_remotes(world.model, pid) is None
    first = world.see(full, branch=pid)
    assert first.project != pid
    _mint, read, found = membership_alias._FIRST[world.store][
        (world.store.store_id, pid)
    ]
    assert found is None
    reads.clear()
    assert membership_alias._first_remotes(world.model, pid) is None
    ((kind, since, taken),) = reads
    assert (kind, since) == ("checkout_put", read) and taken >= 1
    assert world.see(alias).project == pid
    assert membership_alias._first_remotes(world.model, pid) == frozenset({ALIAS})

    _caught_up(full, alias)
    seen = _alias_sighting(world, alias, {ALIAS: FULL})
    assert seen.project == first.project
    (merged,) = _merges(world)
    assert pid in merged


def test_a_key_the_other_project_holds_only_as_an_alias_is_no_remote_witness(
    world: World,
) -> None:
    """The review's P8: the other project's checkout spells its remote
    ``old.example/acme/lib``, which its machine's rule rewrites to
    ``gh.example/acme/lib``, so its project holds the old key without
    fetching from it. A checkout whose alias fetches from the old URL
    does not meet it there: no checkout of that project fetches from
    exactly what this one does."""
    old = "old.example/acme/lib"
    up = world.repo(_w("q"), remote=old)
    fork = world.clone(up, _w("k"), remote="ol/acme/lib")
    up.commit()
    q = _alias_sighting(world, up, {old: "gh.example/acme/lib"})
    k = world.see(fork)
    assert q.project != k.project
    _caught_up(up, fork)
    for _ in range(2):
        seen = _alias_sighting(world, fork, {"ol/acme/lib": old})
        assert seen.project == k.project
    assert _merges(world) == []


def test_two_projects_fetching_from_the_url_leave_both_apart(world: World) -> None:
    """Where two settled projects fetch from every remote the alias clone
    fetches from (another repository that came to name the URL), and it
    holds commits of both, the evidence does not say which: nothing is
    joined."""
    full, alias, first, second = _split_pair(world)
    other = world.repo(_w("other"), remote="example.invalid/elsewhere")
    assert world.see(other).outcome == NEW
    other.remotes = {FULL}
    world.see(other)
    _caught_up(full, alias)
    alias.history.commits.update(other.history.commits)
    _stays_apart(world, alias, second)


def test_a_project_holding_only_some_of_the_remotes_is_not_counted(
    world: World,
) -> None:
    """The alias clone fetches from two remotes, its alias's URL and an
    upstream; another repository's project fetches from the upstream
    alone. Only the project that fetches from both is named, and the two
    join."""
    up = "github.com/upstream/lib"
    full = world.repo(_w("full"), remote=FULL)
    full.remotes = {FULL, up}
    alias = world.clone(full, _w("alias"), remote=ALIAS)
    full.commit()
    first = world.see(full)
    second = world.see(alias)
    assert (first.outcome, second.outcome) == (NEW, NEW)
    upstream = world.repo(_w("upstream"), remote="example.invalid/elsewhere")
    assert world.see(upstream).outcome == NEW
    upstream.remotes = {up}
    world.see(upstream)
    alias.remotes = {ALIAS, up}
    _caught_up(full, alias)
    assert _alias_sighting(world, alias, {ALIAS: FULL}).project == first.project


# ---------------------------------------------------------------------------
# Settled projects
# ---------------------------------------------------------------------------


def test_the_strict_switch_leaves_the_two_apart(world: World) -> None:
    """Under ``membership.strict`` a join resting on history asks (A3):
    the alias clone's sighting joins nothing."""
    full, alias, first, second = _split_pair(world)
    _caught_up(full, alias)
    res = _alias_sighting(world, alias, {ALIAS: FULL}, strict=True)
    assert res.project == second.project
    assert _merges(world) == []


def test_projects_of_two_tenants_stay_apart(world: World) -> None:
    """The full-URL clone lies under a client's boundary file, the alias
    clone outside it: a sighting's evidence never merges across tenants."""
    tenant = _client(world, "acme", boundary=True)
    full = world.repo("/clients/acme/full", remote=FULL)
    full.files = (tenant,)
    alias = world.clone(full, _w("alias"), remote=ALIAS)
    alias.files = ()
    full.commit()
    first = world.see(full)
    second = world.see(alias)
    assert first.tenant is not None and second.tenant is None
    _caught_up(full, alias)
    _stays_apart(world, alias, second)


def test_an_alias_clones_project_of_another_tenant_stays_apart(world: World) -> None:
    """The alias clone was first seen under a client's boundary file, so
    its project is that client's; seen outside it now, in the full-URL
    clone's tenant, its project is still the client's and is not merged
    out of it."""
    tenant = _client(world, "acme", boundary=True)
    full, alias, first, _second = _split_pair_with(world, alias_files=(tenant,))
    assert world.model.project(str(_second.project)).tenant is not None
    alias.files = ()
    _caught_up(full, alias)
    for _ in range(2):
        seen = _alias_sighting(world, alias, {ALIAS: FULL})
        assert seen.project == _second.project
    assert _merges(world) == []


def test_an_open_question_on_either_project_leaves_the_two_apart(
    world: World,
) -> None:
    """A question open on a checkout of either project is the user's to
    answer: the alias clone's sighting merges nothing while it is open."""
    full, alias, first, second = _split_pair(world)
    _caught_up(full, alias)
    world.model.open_proposal(
        checkout=str(first.checkout),
        kind="legacy",
        candidates=[str(first.project)],
        evidence={},
        reason="legacy",
        line="a question",
    )
    _stays_apart(world, alias, second)


@pytest.mark.parametrize(
    "holder", ["retired", "gone", "live", "elsewhere", "quiet_elsewhere"]
)
@pytest.mark.parametrize("about", ["the_alias_project", "another_project"])
def test_a_question_no_sighting_shows_leaves_the_two_to_join(
    world: World, about: str, holder: str
) -> None:
    """An import's question open on a second checkout of the alias clone's
    project, offering to join the imported project to that project or to
    a third one. Where no sighting shows it there (that checkout retired,
    as ENG-215's heal leaves a record whose project has no live checkout,
    or its folder gone here) it holds nothing: the alias clone's sighting
    merges the two projects, and where the question is about the alias
    clone's project, that sighting asks it, moved to the alias clone's
    checkout; about the third project, it stays, asked at that project's
    next sighting. Where a sighting shows it (``live``), the two stay
    apart while it is open. Before, it held the join, shown at no
    sighting. On another machine (a container's checkout, ``elsewhere``)
    its folder is read there alone, so it holds the join while one of its
    views was sighted within `RETIRE_AFTER`, and holds nothing once none
    was for that long (``quiet_elsewhere``, review R8 4: it held the join
    for good with its folder gone there). The second checkout's views,
    which the rule and the placing of the question read, are read before
    the sighting's transaction (`membership_alias.planned`, review R8 1)."""
    full, alias, first, second = _split_pair(world)
    box = holder.endswith("elsewhere")
    twin = world.clone(
        alias, "/w/alias-twin", remote=ALIAS, machine="M2" if box else world.machine
    )
    other = world.see(twin, model=world.model_for("M2") if box else None)
    assert other.project == second.project and other.checkout != second.checkout
    third = world.repo("/w/third", remote="example.invalid/third")
    x = world.see(third)
    imported = world.see(world.repo("/w/imported", remote="example.invalid/i"))
    target = str(second.project if about == "the_alias_project" else x.project)
    pr = world.model.open_proposal(
        checkout=str(other.checkout),
        kind=IMPORT,
        candidates=[target],
        evidence={
            "imported_project": str(imported.project),
            "source": "export.json",
            "codes": {target: ["H"]},
        },
        reason=IMPORT,
        line="an import's question",
        distinct="imported_project",
    )
    if holder == "retired":
        with world.model._call():
            k = world.model.checkout(str(other.checkout))
            world.model._put_checkout(dataclasses.replace(k, retired=True))
            world.model._event("checkout_retired", checkout=k.id)
    elif holder == "gone":
        del world.disk[(twin.machine, twin.path)]
    elif holder == "quiet_elsewhere":
        with world.model._call():
            k = world.model.checkout(str(other.checkout))
            old = isoformat_utc(world.clock - RETIRE_AFTER - timedelta(days=1))
            for v in k.views:
                world.model._put_view(dataclasses.replace(v, last_seen=old))
    _caught_up(full, alias)
    reads = _reads_in_the_transaction(world)

    seen = _alias_sighting(world, alias, {ALIAS: FULL})

    assert reads == []
    q = world.model.proposal(pr.id)
    if holder in ("live", "elsewhere"):
        assert seen.project == second.project and _merges(world) == []
        assert (q.state, q.checkout) == ("open", other.checkout)
        return
    assert seen.project == first.project and len(_merges(world)) == 1
    if about == "the_alias_project":
        assert (q.state, q.checkout) == ("open", seen.checkout)
        assert any(pr.id in line for line in seen.lines)
        return
    assert (q.state, q.checkout) == ("open", other.checkout)
    assert not any(pr.id in line for line in seen.lines)
    again = world.see(third)
    assert again.checkout == x.checkout and any(pr.id in line for line in again.lines)
    assert world.model.proposal(pr.id).checkout == x.checkout


def test_a_question_the_join_asks_is_placed_by_views_read_before_it(
    world: World,
) -> None:
    """Review R8 1: an import's question offering to join another project
    to the full-URL clone's, open on a checkout of a third project here
    whose folder is gone. The alias clone's sighting merges its project
    into the full-URL clone's, then asks the question at the alias clone,
    now a checkout of that project (`_asked_here`): the gone checkout's
    views are read before the transaction, the projects the rule may
    merge into planned with the alias clone's (`membership_alias.planned`)."""
    full, alias, first, second = _split_pair(world)
    third = world.repo("/w/third", remote="example.invalid/third")
    x = world.see(third)
    imported = world.see(world.repo("/w/imported", remote="example.invalid/i"))
    pr = world.model.open_proposal(
        checkout=str(x.checkout),
        kind=IMPORT,
        candidates=[str(first.project)],
        evidence={
            "imported_project": str(imported.project),
            "source": "export.json",
            "codes": {str(first.project): ["H"]},
        },
        reason=IMPORT,
        line="an import's question",
        distinct="imported_project",
    )
    del world.disk[(third.machine, third.path)]
    _caught_up(full, alias)
    reads = _reads_in_the_transaction(world)

    seen = _alias_sighting(world, alias, {ALIAS: FULL})

    assert reads == []
    assert seen.project == first.project and len(_merges(world)) == 1
    q = world.model.proposal(pr.id)
    assert (q.state, q.checkout) == ("open", seen.checkout)
    assert any(pr.id in line for line in seen.lines)


def test_a_project_file_bound_to_either_project_leaves_the_two_apart(
    world: World,
) -> None:
    """The review's P3: ``project init`` in the alias clone binds its file
    to its project; a file bound to either project pins it."""
    full, alias, first, second = _split_pair(world)
    f = _file(_w("alias"))
    world.model.declare(
        Declaration(
            "init",
            "os:tester",
            checkout=second.checkout,
            file_id=f.id,
            directory=_w("alias"),
        )
    )
    alias.files = (f,)
    assert world.see(alias).project == second.project
    _caught_up(full, alias)
    _stays_apart(world, alias, second)


def test_a_file_bound_by_a_sighting_leaves_the_two_apart(world: World) -> None:
    """A project file the full-URL clone carries, bound at its sighting
    and not by a declaration, pins its project as well."""
    full, alias, first, second = _split_pair(world)
    full.files = (_file(_w("full")),)
    assert world.see(full).project == first.project
    _caught_up(full, alias)
    _stays_apart(world, alias, second)


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def test_a_declared_join_of_the_alias_clone_elsewhere_is_kept(world: World) -> None:
    """The review's P1: ``project join`` from the alias clone into a
    project of another repository merges its project there; the next
    sighting does not merge that project into the full-URL clone's."""
    full, alias, first, second = _split_pair(world)
    other = world.repo(_w("other"), remote="github.com/acme/other")
    po = world.see(other)
    world.model.declare(
        Declaration("join", "os:tester", checkout=second.checkout, target=po.project)
    )
    assert world.see(alias).project == po.project
    _caught_up(full, alias)
    for _ in range(2):
        assert _alias_sighting(world, alias, {ALIAS: FULL}).project == po.project
    assert world.see(other).project == po.project
    assert len(_merges(world)) == 1


def test_a_declared_move_of_the_alias_clone_is_kept(world: World) -> None:
    """The review's P2: ``project join`` moving the alias clone (its
    project kept another checkout) into another repository's project."""
    full, alias, first, second = _split_pair(world)
    twin = world.copy(alias, _w("alias-twin"))
    world.see(twin)
    other = world.repo(_w("other"), remote="github.com/acme/other")
    po = world.see(other)
    world.model.declare(
        Declaration("join", "os:tester", checkout=second.checkout, target=po.project)
    )
    assert world.see(alias).project == po.project
    _caught_up(full, alias)
    for _ in range(2):
        assert _alias_sighting(world, alias, {ALIAS: FULL}).project == po.project
    assert world.see(other).project == po.project
    assert _merges(world) == []


def test_a_declared_separation_is_kept(world: World) -> None:
    """The alias clone was asked at its first sight and answered as a
    project of its own: its sightings do not join it to the other."""
    full = world.repo(_w("full"), remote=FULL)
    first = world.see(full)
    alias = world.clone(full, _w("alias"), remote=ALIAS)
    asked = world.see(alias)
    assert asked.outcome == ASK
    world.model.declare(Declaration("new", "os:tester", checkout=asked.checkout))
    for _ in range(2):
        res = _alias_sighting(world, alias, {ALIAS: FULL})
        assert res.project == asked.project != first.project
    assert _merges(world) == []


def test_a_project_split_off_by_project_new_is_kept(world: World) -> None:
    """``project new`` in the alias clone with no question open: its new
    project is the user's, and stays apart."""
    full, alias, first, second = _split_pair(world)
    world.model.declare(Declaration("new", "os:tester", checkout=second.checkout))
    split = world.see(alias)
    _caught_up(full, alias)
    _stays_apart(world, alias, split)


def test_a_record_the_user_moved_from_the_alias_clone_keeps_the_two_apart(
    world: World,
) -> None:
    """``memory move`` of a record written at the alias clone is the
    user's word about what was written in that checkout, which names no
    project of the two: evidence does not merge its project after it."""
    full, alias, first, second = _split_pair(world)
    written = world.write(second)
    world.model.declare(Declaration("move", "os:tester", record=written, target=GLOBAL))
    _caught_up(full, alias)
    _stays_apart(world, alias, second)


def test_an_undone_join_is_kept_undone(world: World) -> None:
    """A join the user undid (``unjoin``) is not made again by the alias
    clone's next sighting; what was written at the alias clone while it
    was joined stays with the project it joined (the review's P6), as
    the line said."""
    full, alias, first, second = _split_pair(world)
    _caught_up(full, alias)
    joined = _alias_sighting(world, alias, {ALIAS: FULL})
    assert joined.project == first.project
    during = world.write(joined)
    world.model.declare(Declaration("unjoin", "os:tester", target=second.project))
    for _ in range(2):
        assert _alias_sighting(world, alias, {ALIAS: FULL}).project == second.project
    assert len(_merges(world)) == 1
    home = world.model.home(during)
    assert home is not None and home.project == first.project


def test_a_record_the_user_moved_from_the_full_url_clone_keeps_the_two_apart(
    world: World,
) -> None:
    """``memory move`` of a record written at the full-URL clone, which
    fetches from the alias clone's keys, is the user's word about Q's
    checkout as one written at the alias clone is about P's: the two stay
    apart (review R8 3: the full-URL clones are read with the alias
    clone, `_declared`)."""
    full, alias, first, second = _split_pair(world)
    written = world.write(first)
    world.model.declare(Declaration("move", "os:tester", record=written, target=GLOBAL))
    _caught_up(full, alias)
    _stays_apart(world, alias, second)


def test_a_choice_between_the_two_keeps_them_apart(world: World) -> None:
    """A question on a third checkout naming both projects, answered by
    the user with one of them: the user weighed the two, and the alias
    clone's sighting does not merge them (review R8 3: an answer to a
    question naming both counts wherever it was asked)."""
    full, alias, first, second = _split_pair(world)
    third = world.see(world.repo("/w/third", remote="example.invalid/third"))
    pr = world.model.open_proposal(
        checkout=str(third.checkout),
        kind=FIRST_SIGHT,
        candidates=[str(second.project), str(first.project)],
        evidence={},
        reason=FIRST_SIGHT,
        line="which project",
    )
    world.model.declare(
        Declaration(
            "join",
            "os:tester",
            checkout=third.checkout,
            proposal=pr.id,
            target=second.project,
        )
    )
    _caught_up(full, alias)
    for _ in range(2):
        assert _alias_sighting(world, alias, {ALIAS: FULL}).project == second.project
    assert _alias_merges(world) == []


def _alias_merges(world: World) -> list[str]:
    """The merges the rule of this module wrote, its evidence naming the
    alias."""
    return [x for x in _merges(world) if "the alias" in x]


def _about_another(world: World, checkout: str, project: str, kind: str) -> str:
    """A question about another project U offering to join U to `project`,
    opened on `checkout` as its writer opens one: an import's, U an
    imported repository's project, or a folder's that could not be read,
    U the project of the records written there (decision B6). Its id."""
    evidence: dict[str, object]
    if kind == IMPORT:
        u = world.see(world.repo("/w/imported", remote="example.invalid/i")).project
        evidence = {
            "imported_project": str(u),
            "source": "export.json",
            "codes": {project: ["H"]},
        }
    else:
        unread = Location(
            machine=world.machine, cwd="/w/unread", state=UNREADABLE, detail="EACCES"
        )
        home = world.model.home(world.write_unresolved(unread))
        assert home is not None and home.checkout is not None
        evidence = {
            "unresolved_project": home.project,
            "unresolved_checkout": home.checkout,
            "path": "/w/unread",
            "memories": 1,
            "episodes": 0,
            "sessions": [],
        }
    return world.model.open_proposal(
        checkout=checkout,
        kind=kind,
        candidates=[project],
        evidence=evidence,
        reason=kind,
        line="a question about another project",
        distinct=_SUBJECT[kind],
    ).id


@pytest.mark.parametrize("answer", ["join", "new"])
@pytest.mark.parametrize("kind", [IMPORT, LEGACY])
@pytest.mark.parametrize("on", ["the_alias_clone", "another_checkout"])
def test_an_answer_about_another_project_leaves_the_two_to_join(
    world: World, on: str, kind: str, answer: str
) -> None:
    """Review R8 3: an import's question, or one about the records of a
    folder that could not be read, about another project U, offering to
    join U to the alias clone's project P, open on the alias clone or on
    another checkout of P, answered by the user: joined (U merged into P)
    or kept apart. The answer says where U's records go, not that P is
    apart from Q: the alias clone's next sighting merges P into Q, a
    joined U's records with P's. Before, the answer's rows named P's
    checkout and P, and held the join for good (`_declared`)."""
    full, alias, first, second = _split_pair(world)
    holder = second.checkout
    if on == "another_checkout":
        holder = world.see(world.clone(alias, "/w/alias-twin", remote=ALIAS)).checkout
    assert holder is not None
    qid = _about_another(world, holder, str(second.project), kind)
    u = str(world.model.proposal(qid).evidence[_SUBJECT[kind]])
    world.model.declare(Declaration(answer, "os:tester", checkout=holder, proposal=qid))
    assert world.model.proposal(qid).state == "answered"
    _caught_up(full, alias)

    seen = _alias_sighting(world, alias, {ALIAS: FULL})

    assert seen.project == first.project and len(_alias_merges(world)) == 1
    joined = world.model.canonical(u) == first.project
    assert joined == (answer == "join")


@pytest.mark.parametrize("how", ["absorb", "join_its_checkout"])
def test_another_project_joined_to_the_alias_clones_leaves_the_two_to_join(
    world: World, how: str
) -> None:
    """Review R8 3, a join of another project U into P, which does not
    count: ``join_project`` (an absorb) at the alias clone naming U, or
    ``project join`` at U's own checkout naming P. Either is the user's
    word about U, where its records go, and the merge keeps them with
    P's, so the alias clone's next sighting merges P into Q, U with it. A
    declaration about the alias clone itself holds the two apart still
    (the tests above)."""
    full, alias, first, second = _split_pair(world)
    other = world.repo("/w/other", remote="example.invalid/other")
    u = world.see(other)
    if how == "absorb":
        decl = Declaration(
            "absorb", "os:tester", checkout=second.checkout, target=u.project
        )
    else:
        decl = Declaration(
            "join", "os:tester", checkout=u.checkout, target=second.project
        )
    world.model.declare(decl)
    assert world.model.canonical(u.project) == second.project
    _caught_up(full, alias)

    seen = _alias_sighting(world, alias, {ALIAS: FULL})

    assert seen.project == first.project and len(_alias_merges(world)) == 1
    assert world.see(other).project == first.project


# ---------------------------------------------------------------------------
# On git: the issue's scenario, the repository's own rule, the cost
# ---------------------------------------------------------------------------

_URL = "https://example.invalid/acme/lib.git"
_RULE = '[url "https://example.invalid/"]\n\tinsteadOf = gh:\n'

_Lab = tuple[Store, MachineIdentity, Config]


def _pair_on_git(tmp_path: Path, lab: _Lab) -> tuple[Path, Path, str, str]:
    """A full-URL clone and its alias clone kept as two projects, as
    before rc14: no rule names the alias yet, and the full-URL clone is
    seen a commit ahead of the alias clone."""
    store, identity, config = lab
    full = make_repo(tmp_path / "full", commits=2, remote=_URL)
    alias = tmp_path / "alias"
    git(tmp_path, "clone", "-q", str(full), str(alias))
    git(alias, "remote", "set-url", "origin", "gh:acme/lib")
    git(full, "commit", "-q", "--allow-empty", "-m", "ahead")
    q = resolve_here(store, config, str(full), identity=identity)
    p = resolve_here(store, config, str(alias), identity=identity)
    assert (q.outcome, p.outcome) == (NEW, NEW)
    assert q.project is not None and p.project is not None
    assert q.project != p.project
    return full, alias, str(q.project), str(p.project)


def _fetch(alias: Path, full: Path) -> None:
    git(alias, "fetch", "-q", str(full), "main")
    git(alias, "merge", "-q", "--ff-only", "FETCH_HEAD")


def _global_rule(text: str) -> None:
    """`text` added to the global config `neutral_git` names."""
    with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as fh:
        fh.write(text)


@_ON_GIT
def test_the_issues_alias_clone_and_full_url_clone_become_one_project(
    tmp_path: Path, lab: _Lab
) -> None:
    """ENG-207's scenario on git: an alias clone and a full-URL clone kept
    as two projects, then a global rule naming the alias and a fetch. The
    alias clone's next sighting joins its project to the full-URL clone's,
    and a third clone joins it without a question, where 9.0.0rc18 kept
    the two and asked the third."""
    store, identity, config = lab
    full, alias, q, p = _pair_on_git(tmp_path, lab)
    _global_rule(_RULE)
    _fetch(alias, full)
    seen = resolve_here(store, config, str(alias), identity=identity)
    assert str(seen.project) == q
    third = tmp_path / "third"
    git(tmp_path, "clone", "-q", str(full), str(third))
    git(third, "remote", "set-url", "origin", _URL)
    res = resolve_here(store, config, str(third), identity=identity)
    assert (res.outcome, str(res.project)) == (SILENT, q)
    assert str(resolve_here(store, config, str(full), identity=identity).project) == q
    cap = location.capture(str(alias), write_token=False)
    assert cap.alias_keys == ("gh/acme/lib",)


@_ON_GIT
def test_a_question_asked_before_the_join_is_answered_by_it_on_git(
    tmp_path: Path, lab: _Lab
) -> None:
    """The asked state on git: the alias clone seen under the strict
    switch, as rc14 saw it, then a third clone asked between the two; the
    alias clone's next sighting joins them and the third clone's next
    joins with no question left."""
    store, identity, config = lab
    full, alias, q, p = _pair_on_git(tmp_path, lab)
    _global_rule(_RULE)
    _fetch(alias, full)
    strict = dataclasses.replace(
        config, membership=dataclasses.replace(config.membership, strict=True)
    )
    assert str(resolve_here(store, strict, str(alias), identity=identity).project) == p
    third = tmp_path / "third"
    git(tmp_path, "clone", "-q", str(full), str(third))
    git(third, "remote", "set-url", "origin", _URL)
    asked = resolve_here(store, config, str(third), identity=identity)
    assert asked.outcome == ASK
    assert str(resolve_here(store, config, str(alias), identity=identity).project) == q
    res = resolve_here(store, config, str(third), identity=identity)
    assert (res.outcome, str(res.project)) == (SILENT, q)


@_ON_GIT
def test_a_rule_in_the_repositorys_own_config_makes_no_alias(
    tmp_path: Path, lab: _Lab
) -> None:
    """The review's R1: the alias clone's own ``.git/config`` names the
    full URL for its remote. That rule is the checkout's own files' word,
    not the machine's: the capture names no alias key, and the two
    projects stay apart."""
    store, identity, config = lab
    full, alias, q, p = _pair_on_git(tmp_path, lab)
    git(alias, "config", f"url.{_URL[: -len('acme/lib.git')]}.insteadOf", "gh:")
    _fetch(alias, full)
    cap = location.capture(str(alias), write_token=False)
    assert cap.alias_keys == ()
    assert "example.invalid/acme/lib" in cap.remote_keys
    for _ in range(2):
        assert (
            str(resolve_here(store, config, str(alias), identity=identity).project) == p
        )


@_ON_GIT
@pytest.mark.parametrize("spelling", ["gh:acme/lib", _URL])
def test_a_checkout_re_pointed_at_the_url_stays_apart_on_git(
    tmp_path: Path, lab: _Lab, spelling: str
) -> None:
    """P9 on git: a checkout with a remote of its own, seen as a new
    project under the global rule, fetches the full-URL clone's commits
    and is re-pointed (``git remote set-url``) at its URL, spelled as the
    URL or as the alias the rule expands to it: either way its project
    stays apart."""
    store, identity, config = lab
    _global_rule(_RULE)
    full = make_repo(tmp_path / "full", commits=2, remote=_URL)
    mine = tmp_path / "mine"
    git(tmp_path, "init", "-q", str(mine))
    git(mine, "commit", "-q", "--allow-empty", "-m", "own")
    git(mine, "remote", "add", "origin", "https://example.invalid/me/mine.git")
    q = resolve_here(store, config, str(full), identity=identity)
    p = resolve_here(store, config, str(mine), identity=identity)
    assert (q.outcome, p.outcome) == (NEW, NEW)
    git(mine, "fetch", "-q", str(full), "main")
    git(mine, "reset", "-q", "--hard", "FETCH_HEAD")
    git(mine, "remote", "set-url", "origin", spelling)
    assert location.capture(str(mine), write_token=False).alias_keys == (
        ("gh/acme/lib",) if spelling != _URL else ()
    )
    for _ in range(2):
        assert resolve_here(store, config, str(mine), identity=identity).project == (
            p.project
        )
    assert [r for r in store.log_rows() if r.kind == "project_merged"] == []


@_ON_GIT
def test_e16_an_alias_clone_that_never_joins_costs_what_any_checkout_does(
    tmp_path: Path,
    lab: _Lab,
    counted: Callable[[Callable[[], object]], _Counts],
) -> None:
    """The review's item 7: an alias clone behind the full-URL clone,
    holding none of its witness commits, is left as it is at each
    sighting. After its first, a steady prompt there spawns no git and
    makes the stat calls of a checkout with no alias (E16: 10, and one
    for each config file read for its remotes; 2 lstat)."""
    store, identity, config = lab
    full, alias, q, p = _pair_on_git(tmp_path, lab)
    _global_rule(_RULE)
    for _ in range(2):
        assert (
            str(resolve_here(store, config, str(alias), identity=identity).project) == p
        )
    files = location.capture(str(alias), write_token=False).config_files
    warm = counted(lambda: resolve_here(store, config, str(alias), identity=identity))
    print(
        f"steady alias clone: spawns={warm.spawns} stat={warm.stats} lstat={warm.lstats}"
    )
    assert warm.spawns == 0
    assert warm.stats <= _BASE_STATS + len(files) and warm.lstats <= 2
    assert warm.readlinks == 0  # GIT_CONFIG_NOSYSTEM: no system answer read

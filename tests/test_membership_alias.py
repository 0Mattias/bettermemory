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
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from bettermemory import location
from bettermemory.config import Config
from bettermemory.location import resolve_here
from bettermemory.machine import MachineIdentity
from bettermemory.membership import ASK, GLOBAL, NEW, SILENT, Declaration, Resolution
from bettermemory.membership_records import ProjectFileRef
from bettermemory.store import Store

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
    full = world.repo("/w/full", remote=FULL)
    alias = world.clone(full, "/w/alias", remote=ALIAS, files=alias_files)
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
    third = world.clone(full, "/w/third", remote=FULL)
    res = world.see(third)
    assert (res.outcome, res.project, res.rule) == (SILENT, first.project, "join:H+R")
    fourth = world.clone(full, "/w/fourth", remote=ALIAS)
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
    third = world.clone(full, "/w/third", remote=FULL)
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
    upstream = world.repo("/w/upstream", remote="github.com/acme/lib")
    fork = world.clone(upstream, "/w/fork", remote="github.com/other/lib")
    fork.commit()
    forked = world.see(fork)
    up = world.see(upstream)
    assert (forked.outcome, up.outcome) == (NEW, NEW)
    upstream.remotes = fork.remotes = {"gh/lib"}
    for _ in range(2):
        a = _alias_sighting(world, upstream, {"gh/lib": "github.com/acme/lib"})
        b = _alias_sighting(world, fork, {"gh/lib": "github.com/other/lib"})
        assert a.project == up.project and b.project == forked.project
    lib = world.repo("/w/lib", remote="gh/acme/lib")
    app = world.repo("/w/app", remote="gh/acme/app")
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
    up = world.repo("/w/q", remote=old)
    fork = world.clone(up, "/w/k", remote="ol/acme/lib")
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
    other = world.repo("/w/other", remote="example.invalid/elsewhere")
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
    full = world.repo("/w/full", remote=FULL)
    full.remotes = {FULL, up}
    alias = world.clone(full, "/w/alias", remote=ALIAS)
    full.commit()
    first = world.see(full)
    second = world.see(alias)
    assert (first.outcome, second.outcome) == (NEW, NEW)
    upstream = world.repo("/w/upstream", remote="example.invalid/elsewhere")
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
    alias = world.clone(full, "/w/alias", remote=ALIAS)
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


def test_a_project_file_bound_to_either_project_leaves_the_two_apart(
    world: World,
) -> None:
    """The review's P3: ``project init`` in the alias clone binds its file
    to its project; a file bound to either project pins it."""
    full, alias, first, second = _split_pair(world)
    f = _file("/w/alias")
    world.model.declare(
        Declaration(
            "init",
            "os:tester",
            checkout=second.checkout,
            file_id=f.id,
            directory="/w/alias",
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
    full.files = (_file("/w/full"),)
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
    other = world.repo("/w/other", remote="github.com/acme/other")
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
    twin = world.copy(alias, "/w/alias-twin")
    world.see(twin)
    other = world.repo("/w/other", remote="github.com/acme/other")
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
    full = world.repo("/w/full", remote=FULL)
    first = world.see(full)
    alias = world.clone(full, "/w/alias", remote=ALIAS)
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

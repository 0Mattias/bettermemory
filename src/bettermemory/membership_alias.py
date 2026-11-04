"""An alias clone joins the project of the URL its alias stands for (ENG-207).

Before 9.0.0rc14 the capture keyed a remote spelled with a
``url.<base>.insteadOf`` alias (``gh:acme/lib``) as written, so a clone of
the alias and a clone of the URL it stands for never met by their remote,
and could be kept as two projects: the second one seen was asked (its
history alone is one witness) and kept apart where nobody answered, or
both were new where neither held the other's witness commits when first
seen. Since rc14 a remote is keyed as written and as git fetches it, so
the alias clone's sightings record the full URL's key on its project too,
and a third clone of either then names both projects with two witnesses
each: the ladder asks which.

`rejoin` merges the alias clone's project P into one other project Q at
a sighting of the alias clone, where the two are one repository by the
ladder's own bar for a join, two independent witnesses (rung 4), and
nothing a user said or a file binds stands against it:

* the sighting is of a known checkout, born and outside the strict
  switch, whose capture names an alias key (`Location.alias_keys`): a
  remote spelled with an ``insteadOf`` prefix of the system or the global
  config that git rewrites to another key; a rule in the repository's
  own config, which the checkout's own files set, makes none
  (`location_git.read_remotes`);
* P and Q are settled (`_settled`): neither provisional, a part, a folder
  project or a boundary, both in the location's tenant; no question is
  open on a checkout of either, and no project file is bound to either;
* Q is the one settled project besides P that holds every remote the
  checkout fetches from (its keys less its alias keys; a provisional
  project, such as a third clone's that was asked, is not counted), and
  one of Q's checkouts names exactly those keys, so Q fetches from them
  and does not hold them only as an alias of its own: the remote witness
  R, read on the URL git fetches on both sides, so repositories whose
  aliases share a prefix or a spelling stay apart;
* no declaration names P, Q, a project merged into either, or a checkout
  of either (`_declared`): a join, a move, an init, a new or separate
  project, an unjoin, an absorb, a declared remote or a record moved
  there; evidence never overrides what a user said; and
* the checkout holds one of Q's witness commits (H), read last.

P then merges into Q (`project_merged`, its evidence naming the alias),
and the line names P and its undo (`membership_lines.line_alias_joined`).
A first-sight question whose candidates the merge made one re-tallies at
its next sighting (`membership_resolve._observe`), so a third clone asked
between the two joins Q. A sighting the rule leaves as it is, is kept for
the checkout's evidence (its project, tenant, HEAD, remotes and alias
keys) in this process (`_PASSED`), so a prompt at an alias clone that
never joins reads nothing more after its first; a declaration found
against a pair is kept for the pair (`_DECLARED`), since the log only
grows.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict, defaultdict
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from . import _caches
from .membership_lines import line_alias_joined
from .membership_records import H, R, Checkout, Location

if TYPE_CHECKING:
    from .membership import Membership

#: Sightings and pairs kept, oldest evicted first.
_KEPT = 1024
#: Words a log row carries where a user's declaration wrote it: a
#: ``declaration:<principal>`` in its evidence or answer, the ``declaration``
#: witness of a declared join, a ``principal``, or a ``declared_`` reason.
_MARKS = ("declaration", "declared", '"principal"')
_LOCK = threading.Lock()
_PASSED: OrderedDict[tuple[Any, ...], None] = OrderedDict()
_DECLARED: OrderedDict[tuple[str, str, str], None] = OrderedDict()


@_caches.register
def _clear() -> None:
    with _LOCK:
        _PASSED.clear()
        _DECLARED.clear()


def _kept(memo: OrderedDict[Any, None], key: Any) -> bool:
    with _LOCK:
        return key in memo


def _keep(memo: OrderedDict[Any, None], key: Any) -> None:
    with _LOCK:
        memo[key] = None
        while len(memo) > _KEPT:
            memo.popitem(last=False)


def rejoin(
    self: Membership, k: Checkout, loc: Location, tenant: str | None
) -> list[str]:
    """Merge the project of the known checkout `k`, sighted at `loc`,
    into the project its remotes, as git fetches them, name, under the
    rule of this module; the line that says so, or nothing."""
    aliases = loc.alias_keys & loc.remote_keys
    fetched = loc.remote_keys - aliases
    if not aliases or not fetched or not loc.born or loc.history is None:
        return []
    pid = self._canon(k.project)
    seen = (
        self._store.store_id,
        k.id,
        pid,
        tenant,
        loc.head,
        tuple(sorted(loc.remote_keys)),
        tuple(sorted(aliases)),
    )
    if _kept(_PASSED, seen):
        return []
    q = _target(self, k, pid, frozenset(fetched), tenant)
    witnesses = (
        []
        if q is None
        else [str(r["commit_id"]).lower() for r in self._witness_rows(q)]
    )
    if q is None or not self._holds(loc, witnesses):
        _keep(_PASSED, seen)
        return []
    p = self.project(pid)
    self._merge(
        pid,
        q,
        evidence=(
            "evidence:H+R, the alias "
            + ", ".join(sorted(aliases))
            + " fetched from "
            + ", ".join(sorted(fetched))
        ),
    )
    return [
        line_alias_joined(
            loc.root or loc.cwd, self.project(q).name, q, p.name, pid, (H, R)
        )
    ]


def _target(
    self: Membership,
    k: Checkout,
    pid: str,
    fetched: frozenset[str],
    tenant: str | None,
) -> str | None:
    """The project Q the rule merges P, the checkout `k`'s project
    `pid`, into, or None: every condition of the module but H."""
    if not _settled(self, pid, tenant):
        return None
    holders: dict[str, set[str]] = defaultdict(set)
    for row in self._store.membership_rows(
        "project_remotes", column_in=("remote_key", sorted(fetched))
    ):
        q = self._canon(str(row["project"]))
        if q != pid and self.project(q).provisional is None:
            holders[q].add(str(row["remote_key"]))
    named = [q for q, keys in holders.items() if keys >= fetched]
    if len(named) != 1 or not _settled(self, named[0], tenant):
        return None
    q = named[0]
    held = self._checkouts_by_project([pid, q])
    if not any(set(c.remotes) == fetched for c in held.get(q, ())):
        return None
    checkouts = {c.id for cs in held.values() for c in cs} | {k.id}
    if any(pr.checkout in checkouts for pr in self._open_proposals()):
        return None
    members = {*self._members(pid), *self._members(q)}
    if self._store.membership_rows(
        "project_files", column_in=("project", sorted(members))
    ):
        return None
    if _declared(self, pid, q, members | checkouts):
        return None
    return q


def _settled(self: Membership, pid: str, tenant: str | None) -> bool:
    """Whether project `pid` is one a sighting's evidence may merge: no
    question open on it, in the location's tenant, and no part, folder
    project or boundary, which a declaration or a project file made."""
    p = self.project(pid)
    return (
        p.provisional is None
        and p.tenant == tenant
        and p.parent is None
        and not p.folder
        and not p.boundary
    )


def _declared(self: Membership, pid: str, q: str, ids: Collection[str]) -> bool:
    """Whether a user's declaration names one of `ids` (the projects that
    stand for `pid` and `q`, and their checkouts): a row of the log a
    declaration wrote (`_MARKS`) with one of them among its values. A
    pair found so is kept (`_DECLARED`)."""
    pair = (self._store.store_id, pid, q)
    if _kept(_DECLARED, pair):
        return True
    wanted = set(ids)
    for row in self._store.membership_log():
        if not any(mark in row.payload for mark in _MARKS):
            continue
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if _values(payload) & wanted:
            _keep(_DECLARED, pair)
            return True
    return False


def _values(raw: Any) -> set[str]:
    """Every string value in `raw`, a row's payload, at any depth."""
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, dict):
        return {v for x in raw.values() for v in _values(x)}
    if isinstance(raw, list):
        return {v for x in raw for v in _values(x)}
    return set()

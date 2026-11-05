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
* P was minted on that alias key as written (`_minted_on`): the first
  checkout row the log holds for P, the sighting's that minted it (or
  first joined it, where a migration or an import minted it with no
  checkout), names the alias key and none of the keys the checkout
  fetches from, as a capture before rc14 keyed it. A project minted on a remote of its own,
  or on the alias expanded, is no such split: its remote re-spelled with
  an alias later merges nothing, as it would re-pointed at the URL, so a
  remote's spelling decides no merge;
* P and Q are settled (`_settled`): neither provisional, a part, a folder
  project or a boundary, both in the location's tenant; no question is
  open on a checkout of either where a sighting shows it (one on a
  retired checkout, on one of this machine's whose folder is gone, or on
  another machine's none of whose views was sighted for `RETIRE_AFTER`,
  holds nothing, and the sighting that joins asks it where it offers to
  join another project to P's or Q's: `membership_shared._asked_here`,
  ENG-215), and no project file is bound to either. A question that
  holds the join the alias clone's sighting shows, wherever it sits,
  naming its checkout and restated for its folder, so its calls answer
  it from there as from its own and its commands name the folder they
  run in (`_held`, ENG-215): one on a container's checkout whose folder
  is gone there holds it until `RETIRE_AFTER` has passed since its last
  sighting, shown at no sighting of its own. A question is said to hold
  the join only where no declaration does (the next condition, read
  before the questions);
* Q is the one settled project besides P that holds every remote the
  checkout fetches from (its keys less its alias keys; a provisional
  project, such as a third clone's that was asked, is not counted), and
  one of Q's checkouts names exactly those keys, so Q fetches from them
  and does not hold them only as an alias of its own: the remote witness
  R, read on the URL git fetches on both sides, so repositories whose
  aliases share a prefix or a spelling stay apart;
* no declaration of the user's says which project P or Q is
  (`_declared`): a join, a move or a separation of an alias clone of P
  (any of them, not the one sighted alone: `_alias_clones`) or of a
  full-URL clone of Q, a part declared below one, an answer at one to a
  question about its own project, an answer to a question naming both P
  and Q, a merge or an unjoin of the project one of them is recorded
  in, or a record written at a full-URL clone moved into P, the one move
  the merge would undo (`_moved_into`; a home row's writer is the last
  segment of its rule, so a heal's re-home of a record moved earlier is
  not the user's word); evidence never overrides what a user said. A
  declaration that places another project in P or Q (an answer to an
  import's question or to one about a folder that could not be read, an
  absorb, a join of another project's checkout), and a record moved
  anywhere else, is the user's word about that project or that record,
  which the merge keeps or leaves where it is: it holds nothing
  (ENG-215); and
* the checkout holds one of Q's witness commits (H), read last.

P then merges into Q (`project_merged`, its evidence naming the alias),
and the line names P and its undo (`membership_lines.line_alias_joined`).
A first-sight question whose candidates the merge made one re-tallies at
its next sighting (`membership_resolve._observe`), so a third clone asked
between the two joins Q. A sighting the rule leaves as it is, is kept for
the checkout's evidence (its project, tenant, HEAD, remotes and alias
keys) in this process (`_PASSED`), so a prompt at an alias clone that
never joins reads nothing more after its first; while a question holds
the join, which the sighting after its answer reads again, H is read
once (`_HOLDS`) and the log's declarations only as far as they were not
read (`_SCANNED`), so a steady prompt there reads no file and asks a
container nothing more. A declaration found against a pair is kept for
the pair (`_DECLARED`), and a project's first checkout row for the open
store (`_FIRST`), since the log only grows once committed.
"""

from __future__ import annotations

import json
import threading
import weakref
from collections import OrderedDict, defaultdict
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from . import _caches
from .membership_lines import line_alias_held, line_alias_joined
from .membership_queries import _restatement
from .membership_records import OPEN, H, R, Checkout, Location, Proposal
from .membership_shared import _SUBJECT, _unseen

if TYPE_CHECKING:
    from .membership import Membership

#: Sightings and pairs kept, oldest evicted first.
_KEPT = 1024
#: Words a log row carries where a user's declaration wrote it: a
#: ``declaration:<principal>`` in its evidence or answer, the ``declaration``
#: witness of a declared join, a ``principal``, or a ``declared_`` reason.
_MARKS = ("declaration", "declared", '"principal"')
#: The log rows `_declared` reads, each kind through the log's kind index:
#: a join or a separation that answered no question, a question's answer,
#: a merge and an unjoin, a part's file bound, and a record's home put
#: (a record moved, or the records of a checkout moved with it).
_DECLARING = (
    "home_put",
    "project_file_bound",
    "project_merged",
    "project_unmerged",
    "proposal_answered",
    "resolution_joined",
    "resolution_new",
)
_LOCK = threading.Lock()
_PASSED: OrderedDict[tuple[Any, ...], None] = OrderedDict()
_DECLARED: OrderedDict[tuple[str, str, str], None] = OrderedDict()
#: A held join's sighting whose checkout holds one of Q's witness commits
#: (H), by the sighting's evidence and Q, so the steady prompt of an alias
#: clone a question holds reads H once (review R9 4).
_HOLDS: OrderedDict[tuple[Any, ...], None] = OrderedDict()
#: How far `_declared` read the log for a pair and its anchors finding no
#: declaration, for each open store, as `_FIRST` keeps a read: the next
#: judgment of the pair reads only the rows written since.
_SCANNED: weakref.WeakKeyDictionary[
    Any, OrderedDict[tuple[str, str, frozenset[str]], int]
] = weakref.WeakKeyDictionary()
#: Each project's first checkout row as `_first_remotes` read it, for each
#: open store (a reopened or restored store is another object, and starts
#: empty), keyed by the store's id and the project: the ``project_minted``
#: row's sequence number, the last sequence number read, and the row's
#: remotes once found.
_FIRST: weakref.WeakKeyDictionary[
    Any, OrderedDict[tuple[str, str], tuple[int | None, int, frozenset[str] | None]]
] = weakref.WeakKeyDictionary()


@_caches.register
def _clear() -> None:
    with _LOCK:
        _PASSED.clear()
        _DECLARED.clear()
        _HOLDS.clear()
        _FIRST.clear()
        _SCANNED.clear()


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
    aliases, fetched = _keys(loc)
    if not aliases:
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
    found = _target(self, k, pid, frozenset(fetched), aliases, tenant)
    if found is None:
        _keep(_PASSED, seen)
        return []
    q, holding = found
    if not _kept(_HOLDS, (*seen, q)):
        witnesses = [str(r["commit_id"]).lower() for r in self._witness_rows(q)]
        if not self._holds(loc, witnesses):
            _keep(_PASSED, seen)
            return []
        if holding:
            _keep(_HOLDS, (*seen, q))
    p = self.project(pid)
    if holding:
        # Not kept as passed: the sighting after an answer joins.
        return _held(self, k, loc, p.name, pid, q, holding)
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


def _held(
    self: Membership,
    k: Checkout,
    loc: Location,
    name: str,
    pid: str,
    q: str,
    holding: list[tuple[Proposal, Checkout]],
) -> list[str]:
    """The lines of the questions holding the join of `k`'s project
    `pid` into `q` (`_target`), each open on another checkout of either
    where a sighting shows it, here or on another machine: its id, its
    checkout and where that was last seen, and the question restated for
    that folder from its row (`membership_queries._restatement` with
    `at`): its ``answer_question`` calls answer it from here as from
    there, and each command it offers names the folder it runs in, where
    the question's own line would run it here (review R9 5). It stays
    where it is, asked there too. One open on `k` is named by the
    sighting already (`membership_resolve._caller`). Reads the rows of
    the questions' candidates; no view and no file."""
    target = self.project(q).name
    lines = []
    for pr, c in holding:
        if c.id == k.id:
            continue
        newest = c.newest_view
        path = newest.path if newest is not None else None
        where = (c.machine if c.machine != self.machine else None, path)
        lines.append(
            line_alias_held(
                loc.root or loc.cwd,
                name,
                pid,
                target,
                q,
                pr.id,
                c.id,
                where,
                _restatement(self, pr, at=path or c.id),
            )
        )
    return lines


def _keys(loc: Location) -> tuple[frozenset[str], frozenset[str]]:
    """The alias keys of a sighting the rule reads (`rejoin`) and the
    keys its checkout fetches from; no alias key where it reads none (a
    sighting with no key the fetch rewrites, none it fetches from,
    unborn, or with no history to read H in)."""
    aliases = frozenset(loc.alias_keys & loc.remote_keys)
    fetched = frozenset(loc.remote_keys - aliases)
    if not aliases or not fetched or not loc.born or loc.history is None:
        return frozenset(), fetched
    return aliases, fetched


def planned(
    self: Membership,
    projects: Collection[str],
    loc: Location,
    k: Checkout | None = None,
) -> tuple[set[str], list[Checkout]]:
    """For the liveness read before a resolution's transaction
    (`membership_resolve._liveness_plan`, ENG-156), at a sighting at
    `loc` of the checkout `k`, or of a new one in one of `projects`, that
    the rule reads: the projects it may merge one of them into (those
    holding a key the checkout fetches from), and the checkouts whose
    views `_target` reads (`membership_shared._unseen`), this machine's
    of those projects and of `projects`, not retired and other than `k`,
    where a question is open. Nothing where `loc` names no alias key.
    One read of the projects holding those keys, their checkouts and the
    questions open on this machine's."""
    aliases, fetched = _keys(loc)
    if not aliases:
        return set(), []
    pids = {self._canon(p) for p in projects}
    qs = {
        self._canon(str(row["project"]))
        for row in self._store.membership_rows(
            "project_remotes", column_in=("remote_key", sorted(fetched))
        )
    } - pids
    held = self._checkouts_by_project([*pids, *qs], machine=self.machine)
    mine = {
        c.id: c
        for cs in held.values()
        for c in cs
        if not c.retired and (k is None or c.id != k.id)
    }
    if not mine:
        return qs, []
    rows = self._store.membership_rows(
        "proposals", where={"state": OPEN}, column_in=("checkout", sorted(mine))
    )
    return qs, [mine[c] for c in sorted({str(r["checkout"]) for r in rows})]


def _target(
    self: Membership,
    k: Checkout,
    pid: str,
    fetched: frozenset[str],
    aliases: frozenset[str],
    tenant: str | None,
) -> tuple[str, list[tuple[Proposal, Checkout]]] | None:
    """The project Q the rule merges P, the checkout `k`'s project
    `pid`, into, or None: every condition of the module but H, the
    questions aside; with Q, the questions that hold the join, with their
    checkouts, each open on a checkout of either where a sighting shows
    it (`_held` shows them, and nothing is merged while one is open).
    What the user declared is read before them, so a question is not
    said to hold a join a declaration holds (review R9 3)."""
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
    full = [c for c in held.get(q, ()) if set(c.remotes) == fetched]
    if not full:
        return None
    if not _minted_on(self, pid, aliases, fetched):
        return None
    members = {*self._members(pid), *self._members(q)}
    if self._store.membership_rows(
        "project_files", column_in=("project", sorted(members))
    ):
        return None
    if _declared(self, pid, q, _alias_clones(self, k, pid, held, aliases), full):
        return None
    by_id = {c.id: c for cs in held.values() for c in cs}
    by_id[k.id] = k
    holding = [
        (pr, by_id[pr.checkout])
        for pr in self._open_proposals()
        if pr.checkout is not None
        and pr.checkout in by_id
        and (pr.checkout == k.id or not _unseen(self, by_id[pr.checkout]))
    ]
    return q, holding


def _alias_clones(
    self: Membership,
    k: Checkout,
    pid: str,
    held: dict[str, list[Checkout]],
    aliases: frozenset[str],
) -> list[Checkout]:
    """Every alias clone of P (`pid`): the sighted checkout `k` and each
    of P's checkouts, retired ones included, that names a key P was
    minted on (`_first_remotes`) or one of the sighting's alias keys.
    `_declared` reads what the user said at each of them, not at the
    sighted one alone, so a declaration at one twin holds the pair
    whichever twin is sighted first, as `_DECLARED` keeps it for the pair
    (review R9 1)."""
    keys = (_first_remotes(self, pid) or frozenset()) | aliases
    return [
        k,
        *(c for c in held.get(pid, ()) if c.id != k.id and keys & set(c.remotes)),
    ]


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


def _minted_on(
    self: Membership, pid: str, aliases: Collection[str], fetched: Collection[str]
) -> bool:
    """Whether project `pid` was minted on an alias key of the sighting
    as written, unexpanded, as a capture before rc14 keyed it (or one
    before the system config was read, for a rule there): its first
    checkout row (`_first_remotes`) names one of `aliases` among its
    remotes and none of the keys the checkout fetches from (`fetched`).
    A project minted on another spelling, its remote spelled with an
    alias later, or minted on the alias expanded, is no split this
    repairs: so a remote's spelling decides no merge."""
    first = _first_remotes(self, pid)
    return first is not None and bool(first & set(aliases)) and not first & set(fetched)


def _first_remotes(self: Membership, pid: str) -> frozenset[str] | None:
    """The remotes of the first checkout row the log holds for project
    `pid` from its ``project_minted`` row on: the sighting's that minted
    it, or, for a project a migration or an import minted with no
    checkout, the first sighting's that joined it; None where there is
    none yet. Found, the row is kept for the open store and the project
    (`_FIRST`), since the log only grows. None is kept as how far the log
    was read, not as an answer: a checkout row can arrive later (a clone
    joining a project an import minted), so the next judgment reads only
    the rows written since and finds it, where reading from the mint each
    time cost a project whose first row came late every row after it.
    Nothing read past the last row committed is kept
    (`Store.committed_seq`; the review's P12): a rollback takes the open
    transaction's rows away, and the next writes take their sequence
    numbers and the project's id again, so a row kept from one answered
    for another project, and a read position past them passed over their
    rows."""
    store = self._store
    key = (store.store_id, pid)
    with _LOCK:
        mint, read, first = _FIRST.setdefault(store, OrderedDict()).get(
            key, (None, 0, None)
        )
    if first is not None:
        return first
    since = read
    quoted = json.dumps(pid)
    if mint is None:
        for row in store.membership_log(["project_minted"], since_seq=read):
            read = row.seq
            if quoted in row.payload and _loads(row.payload).get("project") == pid:
                mint, read = row.seq, row.seq - 1
                break
    if mint is not None:
        for row in store.membership_log(["checkout_put"], since_seq=read):
            read = row.seq
            put = _loads(row.payload).get("checkout") if quoted in row.payload else None
            if isinstance(put, dict) and put.get("project") == pid:
                first = frozenset(_values(put.get("remotes_json")))
                break
    kept = _committed(store.committed_seq(), since, (mint, read, first))
    if kept is None:
        return first
    with _LOCK:
        memo = _FIRST.setdefault(store, OrderedDict())
        memo.pop(key, None)
        memo[key] = kept
        while len(memo) > _KEPT:
            memo.popitem(last=False)
    return first


def _committed(
    base: int | None, since: int, found: tuple[int | None, int, frozenset[str] | None]
) -> tuple[int | None, int, frozenset[str] | None] | None:
    """What `_first_remotes` keeps of `found` (the mint's sequence
    number, the last read, the first row's remotes) where no row past
    `base` is committed (`Store.committed_seq`; None: every row is):
    a mint or a first row past it is not kept, and the read position
    stops at it, the rows up to it having held neither. None, nothing to
    keep, where `base` is before where this read began (`since`)."""
    if base is None:
        return found
    if base < since:
        return None
    mint, read, first = found
    if mint is not None and mint > base:
        return (None, base, None)
    if first is not None and read > base:
        return (mint, base, None)
    return (mint, min(read, base), first)


def _loads(payload: str) -> dict[str, Any]:
    """A log row's payload as a mapping; an empty one where it is not."""
    try:
        raw = json.loads(payload)
    except ValueError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _declared(
    self: Membership,
    pid: str,
    q: str,
    clones: Collection[Checkout],
    full: Collection[Checkout],
) -> bool:
    """Whether the user said which project P (`pid`) or Q (`q`) is: a row
    of the log a declaration wrote (`_MARKS`, `_DECLARING`) that is the
    user's word about an alias clone of P (`clones`, `_alias_clones`) or
    a full-URL clone of Q that fetches from its keys (`full`), about the
    project either stands for as a whole, or about the two (`_says`). A
    declaration that places another project, or a checkout of another, in
    P or Q (an answer to an import's question or to a folder's that could
    not be read, an absorb, a join of that checkout) says where that
    project's records go, and the merge keeps them with P's: it does not
    count. A pair found so is kept (`_DECLARED`); where none is found,
    how far the log was read is kept for the pair and its clones
    (`_SCANNED`), up to the last row committed, so the next judgment
    reads the rows written since."""
    pair = (self._store.store_id, pid, q)
    if _kept(_DECLARED, pair):
        return True
    at = {c.id for c in (*clones, *full)}
    own = {c.project for c in (*clones, *full)}
    store = self._store
    key = (pid, q, frozenset(at))
    with _LOCK:
        since = _SCANNED.get(store, OrderedDict()).get(key, 0)
    read = since
    for row in store.membership_log(_DECLARING, since_seq=since):
        read = row.seq
        if not any(mark in row.payload for mark in _MARKS):
            continue
        said = _says(self, row.kind, _loads(row.payload), at, own, (pid, q))
        if said or _moved_into(self, row.kind, _loads(row.payload), pid, full):
            _keep(_DECLARED, pair)
            return True
    base = store.committed_seq()
    with _LOCK:
        memo = _SCANNED.setdefault(store, OrderedDict())
        memo.pop(key, None)
        memo[key] = max(since, read if base is None else min(read, base))
        while len(memo) > _KEPT:
            memo.popitem(last=False)
    return False


def _says(
    self: Membership,
    kind: str,
    payload: dict[str, Any],
    at: Collection[str],
    own: Collection[str],
    both: Collection[str],
) -> bool:
    """Whether a row a declaration wrote is the user's word about P's or
    Q's own identity (`_declared`): a join or a separation (``project
    new``) of a checkout of `at`, an alias clone or a full-URL clone; a
    part declared below its root; an answer at one to a question about
    its own project, of any kind but one about another project
    (`_SUBJECT`); an answer anywhere to a question naming both P and Q
    (`both`), a choice between them; or a merge or an unjoin of the
    project such a checkout's row names (`own`, which a merge leaves and
    a move rewrites). A record moved is read by `_moved_into`. Its rows
    of other kinds, and those naming no declaration, say nothing here."""
    if kind in ("resolution_joined", "resolution_new"):
        return "principal" in payload and payload.get("checkout") in at
    if kind == "project_file_bound":
        return "declaration" in str(payload.get("evidence") or "") and (
            payload.get("checkout") in at
        )
    if kind in ("project_merged", "project_unmerged"):
        return "declaration" in str(payload.get("evidence") or "") and (
            payload.get("project") in own
        )
    if kind != "proposal_answered" or "declaration" not in str(payload.get("by") or ""):
        return False
    try:
        pr = self.proposal(str(payload.get("proposal") or ""))
    except KeyError:
        return False
    if pr.kind in _SUBJECT:
        return False
    named = {self._canon(c) for c in pr.candidates}
    return payload.get("checkout") in at or set(both) <= named


def _moved_into(
    self: Membership,
    kind: str,
    payload: dict[str, Any],
    pid: str,
    full: Collection[Checkout],
) -> bool:
    """Whether a home row is the user's move of a record written at a
    full-URL clone of Q (`full`) into P (`pid`) (``memory move``,
    `membership_declare._move_record`): the one move the merge undoes, as
    it carries P's records into Q, so the user said that record is not
    Q's. A move anywhere else says where that record goes, which the
    merge leaves as it is (to a third project or none) or agrees with
    (into Q), and holds nothing (review R9 2). The row's own writer is the
    last segment of its rule: a heal or a checkout's move that re-homes a
    record moved earlier keeps that move in its rule, which is not the
    word of the row that carries it."""
    if kind != "home_put":
        return False
    home = payload.get("home")
    if not isinstance(home, dict):
        return False
    writer = str(home.get("rule") or "").rsplit("|", 1)[-1]
    return (
        writer.startswith("declaration:")
        and writer.endswith(":move")
        and home.get("checkout") in {c.id for c in full}
        and self._canon(str(home.get("project") or "")) == pid
    )


def _values(raw: Any) -> set[str]:
    """Every string value in `raw`, a row's payload, at any depth."""
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, dict):
        return {v for x in raw.values() for v in _values(x)}
    if isinstance(raw, list):
        return {v for x in raw for v in _values(x)}
    return set()

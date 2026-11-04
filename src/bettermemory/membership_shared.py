"""One git directory that several machines see under other numbers
(ENG-215).

A re-mint names the checkout's token and its git directory's device and
inode number as the store records them for its writer's machine
(`checkout_token.successor`, ENG-205). A Linux bind mount shows a host
and a container one git directory at one device and inode number, so each
machine's record reads the other's re-mint as its own. A share a VM-backed
runtime makes (virtiofs under Docker Desktop and OrbStack, gRPC FUSE)
shows them other numbers, and the container no birth time, so the lineage
a re-mint names is its writer's alone. The rules here read such a token
from the records of the other machines that see the same files:

- `_shared_remint`, for rung 2 (`Membership._continuity`): a token at a
  checkout's own git directory is its re-mint where another machine's
  view of the same files made or recorded it (`_continues`);
- `_view_made`, for B (`membership_resolve._tally`): the same without
  the reading of the object here, as B reads a checkout at another
  object at its path;
- `_restamped`: a checkout's init stamps as its object reads now, where
  birth time or the token proved it, so the files the rules compare
  follow a mode or an extended attribute set on them;
- `_healed` and `_ask_which`: of two records of one git directory, the
  earlier one, where the later one is the second checkout a sighting
  that did not read such a re-mint registered (9.0.0rc18 and earlier
  could not) and no one chose it; a question naming both projects where
  records were written at it since in another project than the first's.

The init stamps are what tells a view from a copy: the name, mtime, ctime
and size of `description`, `info/exclude` and a linked worktree's
`commondir` (`location_git._init_stamps`), each namespace numbering the
inode its own way. No call sets a file's ctime, so a file-level copy has
ctimes of its own; a filesystem snapshot, an overlay's lower layer or a
block-level clone of a VM's disk keeps them. None of the rules reads a
token that is not in the checkout's own git directory: whatever writes
there can already make a token the checkout takes
(`checkout_token.succeeds`), and with birth time the checkout takes any
token found at its object.

Each function takes the model as `self: Membership`, as the ladder's do
(`membership_resolve`); the class binds `_shared_remint` by name. This
module imports the standard library, `checkout_token` and the records,
rows and lines modules, and `membership` only under TYPE_CHECKING.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import checkout_token
from .membership_lines import line_restated
from .membership_records import (
    DUPLICATE_CHECKOUT,
    FILE_CONTRADICTION,
    FIRST_SIGHT,
    Checkout,
    Location,
    _found_token,
)
from .membership_rows import _checkout_from

if TYPE_CHECKING:
    from .membership import Membership

#: The witnesses after which a checkout's init stamps are taken as its
#: object reads them (`_restamped`): its birth time, its token, or its
#: re-mint. The last HEAD, the init stamps themselves and another witness
#: of the object prove less, and leave the stamps as they were.
RESTAMPED_BY = frozenset({"birth", "token", "remint"})
#: Nanoseconds in a second: a share that keeps times to the second
#: (`_same_file`).
_SECOND_NS = 1_000_000_000
#: What `_healed` reads of two records of one git directory: the earlier
#: one, silently (``HEAL``); the later one, with a question naming both
#: projects (``ASK_WHICH``); or the later one as ENG-205's order has it
#: (the empty string).
HEAL = "heal"
ASK_WHICH = "ask"
#: The log rows `_joined_silently_by_token` reads of a checkout, each kind
#: through the log's kind index: its registration, and the rows a
#: declaration writes naming it as their ``checkout`` (a join or a
#: separation that answered no question, an absorb, a part's project file
#: bound in it). A question's answer is read from the question's row
#: (`_asked_where`). The membership tables' mutation rows record no
#: declaration, and the rows of its sightings (``resolution_session``, a
#: view added) are as many as its sessions, so neither is read.
_NAMING = (
    "checkout_registered",
    "project_absorbed",
    "project_file_bound",
    "resolution_joined",
    "resolution_new",
)
#: The questions whose every answer picks or confirms the project of the
#: checkout they are on (`_asked_where`): a first sight's, a contradiction
#: of the root's project file, and a second record's own. The others ask
#: about something else: whether a changed remote is still the project
#: (its separate answer, a project of the checkout's own, re-mints the
#: token, which `_healed` then does not read), a part below the root, the
#: records of a folder that is gone or could not be read, an import.
_PICKING = frozenset({FIRST_SIGHT, FILE_CONTRADICTION, DUPLICATE_CHECKOUT})


def _shared_remint(
    self: Membership, k: Checkout, loc: Location, found: str | None
) -> bool:
    """Whether `found`, a token at `k`'s object that `k` does not record
    and that names no lineage of `k`'s own, is `k`'s re-mint made through
    another machine's view of the same git directory (`_continues`). One
    read of the checkouts' token index, made only where `k`'s stamps are
    read here, with or without their ctimes. Read only for a sighting
    without birth time whose token is neither recorded nor `k`'s own
    re-mint."""
    if not k.token or not k.stamps or not checkout_token.valid(found):
        return False
    if found == k.token or not (_read_here(k, loc) or _moved_under(k, loc)):
        return False
    return _continues(k, loc, found, _carriers(self, (k.token, found)))


def _continues(
    k: Checkout, loc: Location, found: str, carriers: Collection[Checkout]
) -> bool:
    """The rule over `carriers`, the checkouts recording `k`'s token or
    `found`. Either one of `k`'s init stamps is read here, inode number
    included (A2: the object is the recorded git directory), and another
    machine's view of the same files made or recorded `found`
    (`_made_by`); or every one of `k`'s stamps is read here but for its
    ctime (`_moved_under`: a mode, an owner or an extended attribute set
    on the files since `k` last took them) and a record of another
    machine holding `found` read every stamp here exactly, ctime included
    (`_read_as_now`)."""
    if _read_here(k, loc):
        return _made_by(k, found, carriers)
    return _moved_under(k, loc) and _read_as_now(k, loc, found, carriers)


def _read_here(k: Checkout, loc: Location) -> bool:
    """Whether one of `k`'s init stamps is read at the sighting's object,
    inode number included."""
    return bool({tuple(s) for s in k.stamps} & {tuple(s) for s in loc.stamps})


def _moved_under(k: Checkout, loc: Location) -> bool:
    """Whether every one of `k`'s init stamps is read here by name, inode
    number, mtime and size, its ctime left out."""
    if not k.stamps or not loc.stamps:
        return False
    return _loose(k.stamps) <= _loose(loc.stamps)


def _read_as_now(
    k: Checkout, loc: Location, found: str, carriers: Collection[Checkout]
) -> bool:
    """Whether a record of another machine holding `found` read every
    init stamp here by name, mtime, ctime and size (its inode number its
    namespace's own)."""
    here = _exact(loc.stamps)
    for other in carriers:
        if other.machine == k.machine or other.token != found:
            continue
        if here and here <= _exact(other.stamps):
            return True
    return False


def _loose(stamps: Collection[Sequence[object]]) -> set[tuple[object, ...]]:
    return {(s[0], s[1], s[2], s[4]) for s in stamps if len(s) == 5}


def _exact(stamps: Collection[Sequence[object]]) -> set[tuple[object, ...]]:
    return {(s[0], s[2], s[3], s[4]) for s in stamps if len(s) == 5}


def _view_made(self: Membership, k: Checkout, found: str | None) -> bool:
    """Whether a checkout of another machine that saw the same files as
    `k` (`_made_by`) made or recorded `found`: B's reading of a token at
    the checkout's path, its object another (`membership_resolve._tally`).
    One indexed read by token with no views (`_carriers`). False for no
    token, `k`'s own, or a malformed one."""
    if not k.token or not checkout_token.valid(found) or found == k.token:
        return False
    if not k.stamps:
        return False
    return _made_by(k, found, _carriers(self, (k.token, found)))


def _made_by(k: Checkout, found: str, carriers: Collection[Checkout]) -> bool:
    """Whether a checkout among `carriers` of another machine that saw the
    same files as `k` (an init stamp in common by name, mtime, ctime and
    size, `_same_files`) either records `found`, which it read or wrote in
    its view of that git directory, or names the device and inode number
    `found` is a re-mint of `k`'s token over (`checkout_token.succeeds`):
    so `found` was written into `k`'s git directory through that view. A
    record of `k`'s own machine is never one, since a re-mint there names
    `k`'s own lineage."""
    for other in carriers:
        key = other.object_key
        if (
            other.machine == k.machine
            or key is None
            or not _same_files(k.stamps, other.stamps)
        ):
            continue
        if other.token == found:
            return True
        made = checkout_token.lineage(k.token, (key.device, key.inode))
        if checkout_token.succeeds(found, made):
            return True
    return False


def _carriers(self: Membership, tokens: Collection[str]) -> list[Checkout]:
    """The checkouts recording any of `tokens`: one read of the
    checkouts' token index, their views not read (no rule here reads
    them)."""
    rows = self._store.membership_rows(
        "checkouts", column_in=("token", sorted(set(tokens)))
    )
    return [_checkout_from(row, ()) for row in rows]


def _same_files(
    mine: Collection[Sequence[object]], theirs: Collection[Sequence[object]]
) -> bool:
    """Whether two machines' init stamps share a file: one name and size,
    and the same mtime and ctime (`_same_file`). The inode number is left
    out: each namespace numbers it its own way."""
    return any(_same_file(a, b) for a in mine for b in theirs)


def _same_file(a: Sequence[object], b: Sequence[object]) -> bool:
    """One stamped file as two namespaces read it: ``(name, inode, mtime,
    ctime, size)``, the inode left out. The times match to the nanosecond,
    or within a second where either side keeps both to the whole second
    (a share that rounds them, or a filesystem that keeps no finer time):
    the second those times name is the one the other side reads finer."""
    if len(a) != 5 or len(b) != 5 or (a[0], a[4]) != (b[0], b[4]):
        return False
    am, ac, bm, bc = a[2], a[3], b[2], b[3]
    if not (
        isinstance(am, int)
        and isinstance(ac, int)
        and isinstance(bm, int)
        and isinstance(bc, int)
    ):
        return False
    if (am, ac) == (bm, bc):
        return True
    coarse = (am % _SECOND_NS == 0 and ac % _SECOND_NS == 0) or (
        bm % _SECOND_NS == 0 and bc % _SECOND_NS == 0
    )
    return coarse and abs(am - bm) < _SECOND_NS and abs(ac - bc) < _SECOND_NS


def _restamped(self: Membership, k: Checkout, loc: Location, how: str) -> Checkout:
    """`k` with the init stamps its object reads now, where rung 2 found
    it by birth time, its token or its re-mint (`RESTAMPED_BY`) and the
    stamps read are others than the recorded ones: a mode, an owner or an
    extended attribute set on a stamped file, or a hard link made to it,
    moves its ctime, and stamps kept from the first sight would then tell
    the checkout from no view of it (`_shared_remint`). A sighting that
    read no stamps leaves the recorded ones. One checkout row where they
    changed; a kept capture posts the stamps of the read it keeps, so a
    change is taken at the next capture read afresh (a token re-minted
    since is one: the token is in the capture's signature)."""
    if how not in RESTAMPED_BY or not loc.stamps:
        return k
    if {tuple(s) for s in loc.stamps} == {tuple(s) for s in k.stamps}:
        return k
    return self._put_checkout(dataclasses.replace(k, stamps=tuple(loc.stamps)))


def _healed(self: Membership, kept: Checkout, later: Checkout, loc: Location) -> str:
    """What rung 2 reads of `kept`, which `_shared_remint` continues, and
    `later`, a later record of the same object on this machine that
    records the token found (which ENG-205's order would take), where
    `later` is the second checkout a sighting that did not read the other
    machine's re-mint registered (9.0.0rc18 and earlier, which could not;
    or one that read no stamp of `kept` there then). `HEAL` (`kept`,
    silently) where all of these hold and `later` holds no records, or
    holds them in `kept`'s project, where `kept`'s sightings deliver
    them; `ASK_WHICH` (`later`, with a question naming both projects,
    `_ask_which`) where they hold and it holds some in another; else the
    empty string (`later`):

    - `kept` is continued by another machine's re-mint (`_continues`),
      not its own (`checkout_token.succeeds` over its own lineage fails):
      two records after a re-mint of this machine's own are ENG-205's,
      left in its order;
    - `later` is in the project of a checkout of another machine
      recording the token, and no one chose it there (`_chosen`).

    Then the git directory is `kept`'s and the token was written into it
    through another machine's view, so with birth time the sighting that
    registered `later` would have continued `kept`. A healed sighting
    records the token on `kept`, which then comes first in order, so this
    is read once; `later` stays, idle. Reads only: the token index (the
    rule's own read), and where the rest holds the root file's binding,
    `later`'s questions, the log rows naming it and its homes, each
    indexed or filtered on `later` in SQL."""
    found = _found_token(later, loc)
    if found is None or found != later.token or not kept.token or not kept.stamps:
        return ""
    if found == kept.token or checkout_token.succeeds(found, kept.lineage):
        return ""
    if not (_read_here(kept, loc) or _moved_under(kept, loc)):
        return ""
    carriers = _carriers(self, (kept.token, found))
    if not _continues(kept, loc, found, carriers):
        return ""
    projects = {
        self._canon(c.project)
        for c in carriers
        if c.token == found and c.machine != kept.machine
    }
    mine = self._canon(later.project)
    if mine not in projects or _chosen(self, kept, later, loc):
        return ""
    if mine == self._canon(kept.project):
        return HEAL
    if self._store.membership_rows("homes", where={"checkout": later.id}):
        return ASK_WHICH
    return HEAL


def _chosen(self: Membership, kept: Checkout, later: Checkout, loc: Location) -> bool:
    """Whether someone chose `later` where it is: the root's project file
    bound to a project other than `kept`'s (`project init` there), a
    question about its project opened on it (`_asked_where`), or a log
    row naming it that a declaration wrote (`_by_declaration`); or the
    log does not register it by a silent first-sight join with the token
    among its witnesses."""
    rf = loc.root_file
    bound = self._file_binding(rf.id) if rf is not None else None
    if bound is not None and self._canon(bound) != self._canon(kept.project):
        return True
    if _asked_where(self, later.id):
        return True
    return not _joined_silently_by_token(self, later.id)


def _asked_where(self: Membership, checkout: str) -> bool:
    """Whether a question whose answer picks or confirms `checkout`'s
    project (`_PICKING`) was opened on it, open or answered since. One
    indexed read of its questions."""
    rows = self._store.membership_rows("proposals", where={"checkout": checkout})
    return any(row["kind"] in _PICKING for row in rows)


def _joined_silently_by_token(self: Membership, checkout: str) -> bool:
    """Whether the membership log registers `checkout` by a first-sight
    join with the token among its witnesses, and holds no row naming it
    that a declaration wrote (`_by_declaration`). Only the kinds that can
    be one are read (`_NAMING`), and of those the rows naming it are
    filtered in SQL (`Store.membership_log`), so the read visits the rows
    of those kinds and parses only its own."""
    registered = False
    for row in self._store.membership_log(_NAMING, checkout=checkout):
        try:
            payload = json.loads(row.payload)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("checkout") != checkout:
            continue
        if _by_declaration(payload):
            return False
        if row.kind == "checkout_registered":
            rule = str(payload.get("rule") or "")
            codes = rule.split(":", 1)[1].split("+") if rule.startswith("join:") else []
            registered = "T" in codes
    return registered


def _by_declaration(payload: Mapping[str, Any]) -> bool:
    """Whether a log row was written by a declaration: it names its
    principal (a join or a separation, `membership_declare._declared`), or
    its evidence is one (``declaration:<principal>``: an absorb, a part's
    project file bound), or a join's witness is one."""
    if payload.get("principal"):
        return True
    if str(payload.get("evidence") or "").startswith("declaration:"):
        return True
    witnesses = payload.get("witnesses")
    return isinstance(witnesses, list) and "declaration" in witnesses


def _ask_which(self: Membership, later: Checkout, first: Checkout) -> None:
    """The question about `later`, a second record of `first`'s git
    directory that holds records written since in another project
    (`_healed`): the first record's project, where an answer joining it
    moves `later` and its records (`membership_declare._answer_join`), and
    `later`'s own, where joining it keeps `later` and where it stays until
    answered; the separate answer (``project new``) makes `later` and its
    records a project of their own (`membership_declare._answer_new`). One
    per checkout (`_open_proposal`)."""
    theirs, mine = self._canon(first.project), self._canon(later.project)
    named = [(self.project(x).name, x) for x in (theirs, mine)]
    self._open_proposal(
        checkout=later.id,
        kind=DUPLICATE_CHECKOUT,
        candidates=[theirs, mine],
        evidence={"first": first.id},
        reason=DUPLICATE_CHECKOUT,
        line=lambda q: line_restated(q, DUPLICATE_CHECKOUT, DUPLICATE_CHECKOUT, named),
        signature=None,
    )
